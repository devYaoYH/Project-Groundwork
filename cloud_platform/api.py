"""Authenticated control API. Every researcher record passes one ownership gate."""
from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from cloud_platform.auth import Auth
from cloud_platform.control import PlatformControl


def enqueue(control, launch):
    if os.getenv("PLATFORM_BACKEND", "local") == "local":
        return
    from google.cloud import tasks_v2
    from google.api_core.exceptions import AlreadyExists
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(os.environ["GOOGLE_CLOUD_PROJECT"], os.environ["PLATFORM_REGION"],
                               os.environ["PLATFORM_TASK_QUEUE"])
    audience = os.environ["PLATFORM_DISPATCH_AUDIENCE"]
    task = {"name": f"{parent}/tasks/{launch['id']}", "http_request": {
        "http_method": tasks_v2.HttpMethod.POST, "url": audience + "/internal/dispatch",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"launch_id": launch["id"]}).encode(),
        "oidc_token": {"service_account_email": os.environ["PLATFORM_DISPATCH_SA"], "audience": audience}}}
    try:
        client.create_task(parent=parent, task=task)
    except AlreadyExists:
        pass


def dispatch(control, launch_id):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    launch = control.repo.get("launches", launch_id)
    if not launch:
        raise KeyError("launch not found")
    if launch["status"] not in {"QUEUED", "DISPATCHING", "DISPATCHED"} or launch.get("execution_name"):
        return {"dispatched": True}
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.environ["PLATFORM_REGION"]
    job = launch["job_name"]
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    response = AuthorizedSession(credentials).post(
        f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run",
        json={"overrides": {"containerOverrides": [{"env": [{"name": "PLATFORM_LAUNCH_ID", "value": launch_id}]}]}},
        timeout=30)
    response.raise_for_status()
    operation = response.json()
    # A duplicate delivery can create an extra execution; the worker's atomic
    # claim prevents a second runner from spending tokens on the same launch.
    control.repo.claim("launches", launch_id, ["QUEUED", "DISPATCHING", "DISPATCHED"],
                       {"status": "DISPATCHED", "execution_name": operation["name"], "dispatch_status": "SENT"})
    return {"dispatched": True}


def create_app(control=None, auth=None):
    # Lazy creation keeps import/tooling independent of cloud credentials.
    state = {"control": control, "auth": auth}

    def dependencies():
        if state["auth"] is None:
            state["auth"] = Auth.from_env()
        if state["control"] is None:
            state["control"] = PlatformControl()
        return state["control"], state["auth"]

    async def endpoint(request: Request):
        path = request.url.path
        if path == "/api/health":
            return JSONResponse({"ok": True, "source_sha": os.getenv("PLATFORM_SOURCE_SHA", "local")})
        if path == "/":
            return FileResponse(Path(__file__).parent / "static/index.html")
        control, auth = dependencies()
        if path == "/auth/config":
            config_body, cookie = auth.config(request)
            return JSONResponse(config_body, headers={"Set-Cookie": cookie, "Cache-Control": "no-store"})
        if path in {"/auth/google", "/auth/dev"}:
            principal, cookie = await (auth.login_google(request) if path.endswith("google") else auth.login_dev(request))
            return JSONResponse({"user": principal}, headers={"Set-Cookie": cookie, "Cache-Control": "no-store"})
        if path == "/internal/dispatch":
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2 import id_token
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            try:
                identity = await run_in_threadpool(id_token.verify_oauth2_token, token, GoogleRequest(),
                                                  os.environ["PLATFORM_DISPATCH_AUDIENCE"])
                if identity.get("email") != os.environ["PLATFORM_DISPATCH_SA"] or not identity.get("email_verified"):
                    raise ValueError("wrong caller")
            except Exception as exc:
                raise HTTPException(401, "Invalid service identity") from exc
            return JSONResponse(await run_in_threadpool(dispatch, control, (await request.json())["launch_id"]))
        principal = auth.authenticate(request)
        owner = principal["owner_id"]
        if path.startswith("/api/experiments") or path.startswith("/api/launches"):
            await run_in_threadpool(control.repo.reconcile_stale, owner_id=owner)
        if request.method != "GET":
            auth.require_csrf(request)
        if path == "/auth/me":
            return JSONResponse({"user": principal})
        if path == "/auth/logout":
            return JSONResponse({"ok": True}, headers={"Set-Cookie": auth.logout_cookie()})
        if path == "/api/releases":
            return JSONResponse({"releases": await run_in_threadpool(control.releases)})
        if path == "/api/experiments":
            if request.method == "GET":
                return JSONResponse({"experiments": control.repo.list("experiments", owner_id=owner)})
            return JSONResponse(await run_in_threadpool(control.create_experiment, owner, await body(request)), status_code=201)
        identifier = request.path_params.get("identifier", "")
        if path.startswith("/api/experiments/"):
            if path.endswith("/launch"):
                launch = await run_in_threadpool(control.launch, identifier, owner, await body(request))
                await run_in_threadpool(enqueue, control, launch)
                return JSONResponse(launch, status_code=202)
            if path.endswith("/export"):
                return JSONResponse(await run_in_threadpool(control.export, identifier, owner), status_code=201)
            return JSONResponse(control.detail(identifier, owner))
        if path.startswith("/api/launches/"):
            launch = control.owned("launches", identifier, owner)
            if path.endswith("/cancel"):
                if launch["status"] not in {"QUEUED", "DISPATCHED", "RUNNING"}:
                    raise HTTPException(409, "Launch has already ended")
                # Worker polls this field and terminates its entire process group.
                control.repo.claim("launches", identifier, [launch["status"]], {"cancel_requested": True})
                return JSONResponse({"ok": True})
            if launch["status"] == "QUEUED":
                await run_in_threadpool(enqueue, control, launch)
            return JSONResponse({**launch, "attempts": [row for row in control.repo.list("attempts", owner_id=owner,
                experiment_id=launch["experiment_id"]) if row["launch_id"] == identifier]})
        if path.startswith("/api/episodes/"):
            episode = control.owned("episodes", identifier, owner)
            return Response(control.artifacts.get(episode["artifact_json_key"]), media_type="application/json")
        if path.startswith("/api/exports/"):
            record = control.owned("exports", identifier, owner)
            return Response(control.artifacts.get(record["artifact_key"]), media_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="experiment.zip"'})
        raise HTTPException(404, "Not found")

    async def body(request):
        raw = await request.body()
        if len(raw) > 250000:
            raise HTTPException(413, "Request too large")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "Invalid JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected a JSON object")
        return data

    async def error(request, exc):
        status = 404 if isinstance(exc, KeyError) else (exc.status_code if isinstance(exc, HTTPException) else 400)
        message = "Record not found" if status == 404 else (exc.detail if isinstance(exc, HTTPException) else str(exc))
        return JSONResponse({"error": message}, status_code=status)

    async def http_error(request, exc):
        return await error(request, exc)

    routes = [Route("/", endpoint), Route("/api/health", endpoint),
              Route("/auth/config", endpoint), Route("/auth/me", endpoint),
              Route("/auth/{action}", endpoint, methods=["POST"]),
              Route("/internal/dispatch", endpoint, methods=["POST"]),
              Route("/api/releases", endpoint), Route("/api/experiments", endpoint, methods=["GET", "POST"]),
              Route("/api/experiments/{identifier}", endpoint),
              Route("/api/experiments/{identifier}/launch", endpoint, methods=["POST"]),
              Route("/api/experiments/{identifier}/export", endpoint, methods=["POST"]),
              Route("/api/launches/{identifier}", endpoint),
              Route("/api/launches/{identifier}/cancel", endpoint, methods=["POST"]),
              Route("/api/episodes/{identifier}", endpoint),
              Route("/api/exports/{identifier}/download", endpoint)]
    application = Starlette(routes=routes, exception_handlers={KeyError: error, ValueError: error,
                                                              HTTPException: http_error})
    @application.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response
    return application


app = create_app()
