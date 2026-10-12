import json
import os
import subprocess
import sys

import pytest

from a2a_agent.service import read_descriptor
from a2a_engine.remote.joinlink import encode_join_link
from a2a_engine.remote.lobby import EpisodeSession, Registration
from a2a_engine.remote.server import RuntimeManager


@pytest.mark.parametrize("environment,protocol", [("calendar", "a2a-turns/1"), ("word_guess", "a2a-turns/2")])
@pytest.mark.parametrize("source", ["stdin", "env"])
def test_cli_real_join(tmp_path, environment, protocol, source):
    with RuntimeManager((), provisioning_dir=tmp_path) as manager:
        managed = manager.provision({"environment_id": environment, "join_timeout_s": 15,
                                     "agents": [{"runtime": "external", "type": "scripted"}]})
        link = read_descriptor(managed.descriptors[0])["join_link"]
        env = {key: value for key, value in os.environ.items() if not key.startswith("A2A_JOIN_")}
        if source == "env":
            env["A2A_JOIN_LINK"] = link
        child = subprocess.Popen([sys.executable, "-m", "a2a_agent.server", "--join-link"],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stdin.write((link + "\n").encode() if source == "stdin" else b"")
            child.stdin.close()
            session = EpisodeSession({"environment_id": environment, "agents": [{"runtime": "external"}]}, managed)
            observation = {"game_config": {}} if environment == "calendar" else {"role": "guesser"}
            with session.started({0: Registration(observation)},
                                 protocols=frozenset({protocol}), register_timeout_s=5):
                assert managed.episode.seats[0].protocol_version == protocol
            child.wait(timeout=10)
            assert child.returncode == 0
            assert link.encode() not in child.stdout.read() + child.stderr.read()
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


def test_link_only_descriptor_and_mismatch(tmp_path):
    from a2a_engine.remote.seats import EpisodeContext
    episode = EpisodeContext("test", [0])
    value = {"join_url": "http://127.0.0.1:1/episodes/test/join", "seat": 0,
             "environment_id": "calendar", "expires_at": episode.seats[0].join_deadline,
             "join_ticket": episode.seats[0].ticket}
    path = tmp_path / "descriptor.json"
    path.write_text(json.dumps({"join_link": encode_join_link(value)}))
    path.chmod(0o600)
    assert read_descriptor(path)["join_ticket"] == value["join_ticket"]
    result = subprocess.run([sys.executable, "scripts/print_join_link.py", str(path)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.decode().strip() == encode_join_link(value)
    assert not result.stderr
    path.write_text(json.dumps({"join_link": encode_join_link(value), "seat": 1}))
    with pytest.raises(ValueError, match="inconsistent"):
        read_descriptor(path)


def test_expired_print_and_invalid_stdin_are_sanitized(tmp_path):
    import time
    from a2a_engine.remote.seats import EpisodeContext
    episode = EpisodeContext("expired", [0])
    value = {"join_url": "http://127.0.0.1:1/episodes/expired/join", "seat": 0,
             "environment_id": "calendar", "expires_at": time.time() - 1,
             "join_ticket": episode.seats[0].ticket}
    path = tmp_path / "expired.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    result = subprocess.run([sys.executable, "scripts/print_join_link.py", str(path)],
                            capture_output=True, timeout=10)
    assert result.returncode != 0 and not result.stdout
    assert value["join_ticket"].encode() not in result.stderr
    env = {key: value for key, value in os.environ.items() if not key.startswith("A2A_JOIN_")}
    result = subprocess.run([sys.executable, "-m", "a2a_agent.server", "--join-link"],
                            input=b"a2a-join:private-invalid-secret", env=env, capture_output=True, timeout=10)
    assert result.returncode != 0 and not result.stdout
    assert b"private-invalid-secret" not in result.stderr
    result = subprocess.run([sys.executable, "-m", "a2a_agent.server", "--join-link",
                             "a2a-join:private-argv-secret"], env=env, capture_output=True, timeout=10)
    assert result.returncode != 0 and not result.stdout
    assert b"private-argv-secret" not in result.stderr


@pytest.mark.parametrize("unsafe", ["symlink", "public", "oversized", "fifo"])
def test_print_cli_rejects_unsafe_descriptor(tmp_path, unsafe):
    path = tmp_path / "descriptor"
    if unsafe == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("x" * (16385 if unsafe == "oversized" else 1))
        path.chmod(0o644 if unsafe == "public" else 0o600)
        if unsafe == "symlink":
            link = tmp_path / "link"
            link.symlink_to(path)
            path = link
    result = subprocess.run([sys.executable, "scripts/print_join_link.py", str(path)],
                            capture_output=True, timeout=10)
    assert result.returncode != 0 and not result.stdout
    assert b"owner-only" in result.stderr
