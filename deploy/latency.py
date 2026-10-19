"""Persist wall-clock stages and Cloud Build queue/step timings, including failures."""

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from .cloud_images import handoff
    from .common import command
    from .validated_images import write_record
except ImportError:
    from cloud_images import handoff
    from common import command
    from validated_images import write_record

ROOT = Path(".platform-build")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def seconds(start, finish):
    if not start or not finish:
        return None
    return (
        datetime.fromisoformat(finish.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


def timed(label, arguments):
    ROOT.mkdir(exist_ok=True)
    start, clock = stamp(), time.monotonic()
    result = subprocess.run(arguments, check=False)
    write_record(
        ROOT / f"latency-{label}.json",
        {
            "stage": label,
            "started_at": start,
            "finished_at": stamp(),
            "seconds": round(time.monotonic() - clock, 3),
            "exit_code": result.returncode,
        },
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, arguments)


def describe_timings(build):
    return {
        "id": build["id"],
        "status": build["status"],
        "source_sha": build.get("substitutions", {}).get("_SOURCE_SHA"),
        "runtime_source_sha": build.get("substitutions", {}).get("_RUNTIME_SOURCE_SHA"),
        "machine_type": build.get("options", {}).get("machineType", "default"),
        "created_at": build.get("createTime"),
        "started_at": build.get("startTime"),
        "finished_at": build.get("finishTime"),
        "queue_seconds": seconds(build.get("createTime"), build.get("startTime")),
        "execution_seconds": seconds(build.get("startTime"), build.get("finishTime")),
        "steps": [
            {
                "id": step.get("id"),
                "status": step.get("status"),
                "seconds": seconds(
                    step.get("timing", {}).get("startTime"), step.get("timing", {}).get("endTime")
                ),
                "pull_seconds": seconds(
                    step.get("pullTiming", {}).get("startTime"),
                    step.get("pullTiming", {}).get("endTime"),
                ),
            }
            for step in build.get("steps", [])
        ],
    }


def submit(phase, project, region, source, runtime_source, publish_only):
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if checkout != source:
        raise ValueError("Cloud Build source must match the checked-out commit")
    ROOT.mkdir(exist_ok=True)
    substitutions = {
        "_REGION": region,
        "_SOURCE_SHA": source,
        "_RUNTIME_SOURCE_SHA": runtime_source,
    }
    if phase == "publish":
        substitutions.update(handoff(project, region, source))
        substitutions["_PUBLISH_ONLY"] = str(publish_only).lower()
        substitutions["_GOOGLE_OAUTH_CLIENT_ID"] = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    config = "deploy/cloudbuild-candidates.yaml" if phase == "build" else "cloudbuild.yaml"
    started, clock = stamp(), time.monotonic()
    build = None
    try:
        raw = command(
            [
                "builds",
                "submit",
                ".",
                "--async",
                "--project",
                project,
                "--region",
                region,
                "--config",
                config,
                "--service-account",
                f"projects/{project}/serviceAccounts/groundwork-build@{project}.iam.gserviceaccount.com",
                "--gcs-source-staging-dir",
                f"gs://{project}-groundwork-build-source/source",
                "--substitutions",
                ",".join(f"{key}={value}" for key, value in substitutions.items()),
                "--format=json",
                "--quiet",
            ],
            capture=True,
        )
        build = json.loads(raw)
        submitted = time.monotonic()
        # Persist the ID immediately so interrupted polling can be diagnosed.
        write_record(ROOT / f"cloud-{phase}.json", describe_timings(build))
        while build["status"] in ("PENDING", "QUEUED", "WORKING", "STATUS_UNKNOWN"):
            if time.monotonic() - submitted > 2700:
                raise TimeoutError("Cloud Build polling exceeded 45 minutes; inspect its ID")
            time.sleep(5)
            build = json.loads(
                command(
                    [
                        "builds",
                        "describe",
                        build["id"],
                        "--project",
                        project,
                        "--region",
                        region,
                        "--format=json",
                    ],
                    capture=True,
                )
            )
        if build["status"] != "SUCCESS":
            raise RuntimeError(f"Cloud Build {build['id']} ended with {build['status']}")
        if phase == "build":
            with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as output:
                output.write(f"build-id={build['id']}\n")
    finally:
        result = describe_timings(build) if build else {"status": "SUBMISSION_FAILED"}
        result.update(
            stage=f"cloud-{phase}",
            request_started_at=started,
            request_finished_at=stamp(),
            wall_seconds=round(time.monotonic() - clock, 3),
        )
        if build:
            result["submit_seconds"] = round(submitted - clock, 3)
        write_record(ROOT / f"cloud-{phase}.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("label")
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    cloud = sub.add_parser("submit")
    cloud.add_argument("phase", choices=["build", "publish"])
    cloud.add_argument("--project", required=True)
    cloud.add_argument("--region", default="us-central1")
    cloud.add_argument("--source-sha", required=True)
    cloud.add_argument("--runtime-source-sha", required=True)
    cloud.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    if args.operation == "run":
        timed(args.label, args.arguments)
    else:
        submit(
            args.phase,
            args.project,
            args.region,
            args.source_sha,
            args.runtime_source_sha,
            args.publish_only,
        )


if __name__ == "__main__":
    main()
