"""Cloud Run receives structured runner output independently of UI publication."""
import json
from types import SimpleNamespace

import pytest

from cloud_platform.worker import _LiveDetailPublisher


def publisher(tmp_path, *, backend="gcp"):
    lines = []
    control = SimpleNamespace(_line=lambda launch, line: lines.append((launch, line)))
    worker = SimpleNamespace(artifacts=SimpleNamespace(backend=backend))
    path = tmp_path / "runner.log"
    path.write_bytes(b"")
    return _LiveDetailPublisher(worker, control, "launch-uuid", path), path, lines


def records(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_live_and_final_lines_are_mirrored_once_with_execution_context(tmp_path, monkeypatch, capsys):
    mirror, path, lines = publisher(tmp_path)
    monkeypatch.setenv("CLOUD_RUN_JOB", "groundwork-run-runtime-worker")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "groundwork-run-runtime-worker-cqr4w")
    path.write_bytes(b"game started\r\n2026-10-27 01:38:43,566 ERROR expt_runner: API key expired.\npartial")
    mirror.consume_logs()
    live = records(capsys)
    assert [r["message"] for r in live] == ["game started", "2026-10-27 01:38:43,566 ERROR expt_runner: API key expired."]
    assert [r["severity"] for r in live] == ["INFO", "ERROR"]
    assert [r["runner_line"] for r in live] == [1, 2]
    for record in live:
        assert record["component"] == "groundwork.runner"
        assert record["launch_id"] == "launch-uuid"
        assert record["job_name"] == "groundwork-run-runtime-worker"
        assert record["execution_name"].endswith("-cqr4w")
    mirror.consume_logs()
    assert records(capsys) == []
    with path.open("ab") as output:
        output.write(b" line\nfinal line without newline")
    mirror.consume_logs(final=True)
    final = records(capsys)
    assert [r["message"] for r in final] == ["partial line", "final line without newline"]
    mirror.consume_logs(final=True)
    assert records(capsys) == []
    assert [line for _, line in lines] == [r["message"] for r in live + final]
    assert path.read_bytes().endswith(b"final line without newline")


def test_runner_json_cannot_override_launch_or_forge_another_record(tmp_path, capsys):
    mirror, path, lines = publisher(tmp_path)
    forged = '{"launch_id":"another-user","severity":"CRITICAL","message":"injected"}'
    path.write_text(forged + "\n")
    mirror.consume_logs()
    record, = records(capsys)
    assert record["launch_id"] == "launch-uuid" and record["severity"] == "INFO"
    assert record["message"] == forged
    assert lines == [("launch-uuid", forged)]


@pytest.mark.parametrize("character,count", [("\x00", 100000), ("🧪", 20000)])
def test_large_escaped_line_is_split_into_bounded_reconstructable_entries(tmp_path, capsys, character, count):
    mirror, path, lines = publisher(tmp_path)
    line = character * count
    path.write_text(line + "\n")
    mirror.consume_logs(final=True)
    output = records(capsys)
    assert "".join(r["message"] for r in output) == line
    assert [r["line_part"] for r in output] == list(range(1, len(output) + 1))
    assert all(r["line_parts"] == len(output) for r in output)
    assert all(len(json.dumps(r).encode()) < 256 * 1024 for r in output)
    assert lines == [("launch-uuid", line)]


def test_stdout_failure_does_not_interrupt_frontend_logs_or_final_drain(tmp_path, monkeypatch, caplog):
    mirror, path, lines = publisher(tmp_path)
    def broken_stdout(*args, **kwargs):
        raise BrokenPipeError("stdout unavailable")
    monkeypatch.setattr("builtins.print", broken_stdout)
    path.write_text("first\nlast")
    mirror.consume_logs(final=True)
    assert lines == [("launch-uuid", "first"), ("launch-uuid", "last")]
    assert path.read_text() == "first\nlast"
    assert caplog.text.count("runner stdout mirror unavailable") == 1


def test_local_backend_keeps_file_logs_without_hosted_mirroring(tmp_path, capsys):
    mirror, path, lines = publisher(tmp_path, backend="local")
    path.write_text("local runner output\n")
    mirror.consume_logs(final=True)
    assert records(capsys) == []
    assert lines == [("launch-uuid", "local runner output")]


def test_mirror_does_not_depend_on_frontend_log_persistence(tmp_path, capsys, caplog):
    mirror, path, _ = publisher(tmp_path)
    def unavailable(*args):
        raise OSError("control database unavailable")
    mirror.control._line = unavailable
    path.write_text("provider failure\n")
    mirror.consume_logs()
    assert records(capsys)[0]["message"] == "provider failure"
    assert "live runner log read unavailable" in caplog.text
