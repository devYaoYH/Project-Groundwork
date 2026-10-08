import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from a2a_agent.conformance import run_conformance


def test_public_conformance_with_independently_started_process(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    directory = tmp_path / "private-joins"
    with ThreadPoolExecutor() as executor:
        result = executor.submit(run_conformance, f"http://127.0.0.1:{port}", directory)
        end = time.monotonic() + 10
        descriptors = []
        while time.monotonic() < end and not descriptors:
            descriptors = list(directory.glob("*.json"))
            time.sleep(.01)
        assert len(descriptors) == 1
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
        process = subprocess.Popen([sys.executable, "-m", "a2a_agent.server", "--port", str(port),
                                    "--join-descriptor", str(descriptors[0])], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            report = result.result(timeout=30)
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert report["passed"] and report["duplicate_pushes"] >= 5
            assert report["endpoints"] == ["comm", "env"]
            assert report["schema_rejections"] == 1
            assert not list(directory.glob("*.json"))
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
