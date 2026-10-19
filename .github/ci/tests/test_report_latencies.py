"""Skipped jobs and retry attempts must not become fabricated latency measurements."""

import json

import report_latencies as report


def test_skipped_jobs_have_no_duration_and_query_is_scoped_to_exact_attempt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(name, value)
    calls = []

    def api(arguments, **kwargs):
        calls.append(arguments)
        if "/attempts/" not in arguments[-1]:
            return json.dumps({"created_at": "2026-10-19T00:00:00Z", "head_sha": "b" * 40})
        return json.dumps(
            [
                {
                    "jobs": [
                        dict(
                            name="deploy",
                            conclusion="skipped",
                            steps=[],
                            created_at="2026-10-19T00:00:02Z",
                            started_at="2026-10-19T00:00:02Z",
                            completed_at="2026-10-19T00:00:01Z",
                        )
                    ]
                }
            ]
        )

    monkeypatch.setattr(report.subprocess, "check_output", api)
    report.main()
    assert "/runs/123/attempts/2/jobs" in calls[0][-1]
    result = json.loads((tmp_path / "results/latencies/github-jobs.json").read_text())
    assert result["jobs"][0]["seconds"] is None
    assert result["jobs"][0]["queue_seconds"] is None
    assert report.seconds("2026-10-19T00:00:02Z", "2026-10-19T00:00:01Z") is None
