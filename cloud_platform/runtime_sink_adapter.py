"""Stdlib-only fail-closed event adapter injected by the current worker."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def durable_event_write(envelope, sequence):
    uid = envelope["episode_uid"]
    if not isinstance(uid, str) or not uid or uid in {".", ".."} or "/" in uid or "\\" in uid:
        raise ValueError("invalid episode UID")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("invalid event sequence")
    root = Path(os.environ["PLATFORM_EVENT_SPOOL"])
    data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(data).hexdigest()
    name = f"{uid}.{sequence:08d}.{digest}"
    pending = root / "pending" / (name + ".json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=pending.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, pending)
    ack = root / "acks" / (name + ".json")
    deadline = time.monotonic() + float(os.getenv("PLATFORM_EVENT_ACK_TIMEOUT", "60"))
    while time.monotonic() < deadline:
        if ack.exists():
            status = json.loads(ack.read_text())
            if status.get("sha256") != digest or status.get("error"):
                raise RuntimeError("durable event was not acknowledged by infrastructure")
            ack.unlink(missing_ok=True)
            return
        if (root.parent / "cancel.json").exists():
            raise RuntimeError("execution cancelled before durable event acknowledgment")
        time.sleep(.02)
    raise TimeoutError("infrastructure durable event acknowledgment timed out")
