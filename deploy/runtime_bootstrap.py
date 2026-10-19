"""Start the current worker's stdlib bridge inside any pinned science image."""
import argparse
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-dir", default=os.getenv("PLATFORM_EXECUTION_DIR", "/execution"))
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    script = Path(args.execution_dir) / "runtime_supervisor.py"
    deadline = time.monotonic() + 120
    while not script.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("Worker did not publish its runtime bridge within 120 seconds")
        time.sleep(0.2)
    command = [sys.executable, str(script), "--execution-dir", args.execution_dir]
    if args.loop:
        command.append("--loop")
    else:
        command.append("--wait-for-shutdown")
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
