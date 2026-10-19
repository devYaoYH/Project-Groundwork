"""Report complete job/runner times for this exact workflow attempt."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


def seconds(start, finish):
    if not start or not finish:
        return None
    elapsed = (
        datetime.fromisoformat(finish.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()
    return elapsed if elapsed >= 0 else None


def main():
    endpoint = (
        f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
        f"{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}/jobs"
    )
    pages = json.loads(
        subprocess.check_output(
            ["gh", "api", "--paginate", "--slurp", endpoint + "?per_page=100"],
            text=True,
        )
    )
    jobs = [job for page in pages for job in page["jobs"] if job["name"] != "latencies"]
    run = json.loads(
        subprocess.check_output(["gh", "api", endpoint.split("/attempts/")[0]], text=True)
    )
    executed = [job for job in jobs if job["conclusion"] != "skipped" and job.get("started_at")]
    first_start = min((job["started_at"] for job in executed), default=None)
    last_finish = max(
        (job["completed_at"] for job in executed if job.get("completed_at")), default=None
    )
    result = {
        "run_id": os.environ["GITHUB_RUN_ID"],
        "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "source_sha": os.environ["GITHUB_SHA"],
        "head_sha": run["head_sha"],
        "created_at": run["created_at"],
        "initial_wait_seconds": seconds(run["created_at"], first_start),
        "active_span_seconds": seconds(first_start, last_finish),
        "through_gate_or_publish_seconds": seconds(run["created_at"], last_finish),
        "jobs": [
            {
                "name": job["name"],
                "conclusion": job["conclusion"],
                "queue_seconds": (
                    seconds(job["created_at"], job["started_at"])
                    if job["conclusion"] != "skipped"
                    else None
                ),
                "seconds": (
                    seconds(job["started_at"], job["completed_at"])
                    if job["conclusion"] != "skipped"
                    else None
                ),
                "steps": [
                    {
                        "name": step["name"],
                        "conclusion": step["conclusion"],
                        "seconds": seconds(step.get("started_at"), step.get("completed_at")),
                    }
                    for step in job["steps"]
                ],
            }
            for job in jobs
        ],
    }
    root = Path("results/latencies")
    root.mkdir(parents=True, exist_ok=True)
    (root / "github-jobs.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = [
        f"Through validation/publication: {result['through_gate_or_publish_seconds']}s; "
        f"initial wait {result['initial_wait_seconds']}s; active span {result['active_span_seconds']}s.",
        "",
        "| Job | Result | Queue (s) | Running (s) |",
        "|---|---|---:|---:|",
    ]
    for job in result["jobs"]:
        lines.append(
            f"| {job['name']} | {job['conclusion']} | {job['queue_seconds']} | {job['seconds']} |"
        )
    for path in sorted(root.glob("cloud-*.json")):
        cloud = json.loads(path.read_text())
        lines += [
            "",
            f"Cloud {cloud['stage']}: `{cloud.get('id', 'not submitted')}` "
            f"{cloud['status']}; wall {cloud['wall_seconds']}s, "
            f"queue {cloud.get('queue_seconds')}s, execution {cloud.get('execution_seconds')}s.",
        ]
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
        output.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
