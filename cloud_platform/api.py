"""Authenticated control API. Every researcher record passes one ownership gate."""
from __future__ import annotations

import json
import os
import asyncio
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from cloud_platform.auth import Auth
from cloud_platform.control import PlatformControl
from cloud_platform.static import static_response
from cloud_platform.viewer import Viewer


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
            return static_response(request)
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
        viewer = Viewer(control)
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
        if path == "/api/environments":
            return JSONResponse({"environments": await run_in_threadpool(viewer.environments, owner)})
        if path.startswith("/api/environments/"):
            operation = request.path_params.get("operation", "detail")
            return JSONResponse(await run_in_threadpool(viewer.environment,
                request.path_params["identifier"], operation, request.query_params,
                await body(request) if request.method == "POST" else None))
        if path == "/api/agent-pool":
            return JSONResponse(await run_in_threadpool(viewer.agent_pool))
        if path == "/api/designs/validate":
            return JSONResponse(await run_in_threadpool(viewer.validate, await body(request)))
        if path == "/api/experiments":
            if request.method == "GET":
                return JSONResponse({"experiments": control.repo.list("experiments", owner_id=owner)})
            return JSONResponse(await run_in_threadpool(control.create_experiment, owner, await body(request)), status_code=201)
        identifier = request.path_params.get("identifier", "")
        if path == "/api/launches":
            if request.method == "GET":
                return JSONResponse({"launches": control.repo.list("launches", owner_id=owner)})
            payload = await body(request)
            if payload.get("shard_count") not in {None, 1} or payload.get("shard_index") not in {None, 0}:
                raise ValueError("Launch sharding is not enabled; choose a bounded experiment instead")
            if payload.get("max_parallelism") is None:
                payload["max_parallelism"] = 1
            launch = await run_in_threadpool(control.launch, str(payload.get("experiment_id") or ""), owner, payload)
            await run_in_threadpool(enqueue, control, launch)
            return JSONResponse(launch, status_code=202)
        if path.startswith("/api/experiments/"):
            if path.endswith("/launch"):
                launch = await run_in_threadpool(control.launch, identifier, owner, await body(request))
                await run_in_threadpool(enqueue, control, launch)
                return JSONResponse(launch, status_code=202)
            if path.endswith("/export"):
                return JSONResponse(await run_in_threadpool(control.export, identifier, owner), status_code=201)
            if path.endswith("/design"):
                return JSONResponse(await run_in_threadpool(control.save_design, identifier, owner, await body(request)))
            if path.endswith("/fork"):
                return JSONResponse(await run_in_threadpool(control.fork_design, identifier, owner, await body(request)), status_code=201)
            if path.endswith("/lock"):
                return JSONResponse(await run_in_threadpool(control.lock_design, identifier, owner, await body(request)))
            return JSONResponse(await run_in_threadpool(viewer.experiment, identifier, owner))
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
            if path.endswith("/events"):
                last = int(request.headers.get("last-event-id", request.query_params.get("after_id", "0")))
                async def events():
                    cursor = last
                    for _ in range(20):
                        detail = await run_in_threadpool(viewer.launch, identifier, owner)
                        for event in detail["runner_logs"]:
                            if int(event["id"]) > cursor:
                                cursor = int(event["id"])
                                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                        yield f"event: launch.status\ndata: {json.dumps(detail['launch'])}\n\n"
                        if detail["status"] not in {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "CANCELLING"}:
                            return
                        await asyncio.sleep(1)
                return StreamingResponse(events(), media_type="text/event-stream")
            return JSONResponse(await run_in_threadpool(viewer.launch, identifier, owner))
        if path == "/api/episodes":
            return JSONResponse(await run_in_threadpool(viewer.episodes, owner, request.query_params))
        if path.startswith("/api/episodes/"):
            operation = request.path_params.get("operation", "detail")
            method = {"detail": viewer.episode, "raw": viewer.raw_episode,
                      "observability": viewer.observability, "artifacts": viewer.artifacts}.get(operation)
            if method is None:
                raise KeyError("record not found")
            return JSONResponse(await run_in_threadpool(method, identifier, owner))
        if path.startswith("/api/streams/"):
            return JSONResponse(await run_in_threadpool(viewer.stream, identifier, owner,
                                                       request.path_params.get("operation") == "trace"))
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
              Route("/api/environments", endpoint),
              Route("/api/environments/{identifier}", endpoint),
              Route("/api/environments/{identifier}/{operation}", endpoint, methods=["GET", "POST"]),
              Route("/api/agent-pool", endpoint), Route("/api/designs/validate", endpoint, methods=["POST"]),
              Route("/api/experiments/{identifier}", endpoint),
              Route("/api/experiments/{identifier}/launch", endpoint, methods=["POST"]),
              Route("/api/experiments/{identifier}/export", endpoint, methods=["POST"]),
              Route("/api/experiments/{identifier}/design", endpoint, methods=["POST"]),
              Route("/api/experiments/{identifier}/fork", endpoint, methods=["POST"]),
              Route("/api/experiments/{identifier}/lock", endpoint, methods=["POST"]),
              Route("/api/launches", endpoint, methods=["GET", "POST"]),
              Route("/api/launches/{identifier}", endpoint),
              Route("/api/launches/{identifier}/cancel", endpoint, methods=["POST"]),
              Route("/api/launches/{identifier}/events", endpoint),
              Route("/api/episodes", endpoint),
              Route("/api/episodes/{identifier}", endpoint),
              Route("/api/episodes/{identifier}/{operation}", endpoint),
              Route("/api/streams/{identifier}", endpoint),
              Route("/api/streams/{identifier}/{operation}", endpoint),
              Route("/api/exports/{identifier}/download", endpoint)]
    routes.append(Route("/{path:path}", static_response, methods=["GET", "HEAD"]))
    application = Starlette(routes=routes, exception_handlers={KeyError: error, ValueError: error,
                                                              HTTPException: http_error})
    @application.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response
    return application


app = create_app()
