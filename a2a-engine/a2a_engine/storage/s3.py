"""S3 trace store — lifted from ``expt_runner.run_experiment._write_metadata_and_upload``.

Key layout is preserved exactly so the existing calendar bucket
(``s3://<bucket>/<prefix>/<uploader>/<experiment>/<run_id>/``) keeps working and
already-uploaded traces stay addressable:

    <prefix>/<uploader>/<experiment_name>/<experiment_run_id>/<game_id>.json
    <prefix>/<uploader>/<experiment_name>/<experiment_run_id>/<game_id>.manifest.json

Uploads go through the AWS CLI, matching the previous implementation, so the
same profile/credential setup collaborators already have continues to apply.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from a2a_engine.manifest import RunManifest
from a2a_engine.schemas import GameTraceBase
from a2a_engine.storage.base import StoreCheck, register_store
from a2a_engine.storage.local import LocalJSONStore

log = logging.getLogger("a2a_engine.storage.s3")


class S3TraceStore:
    """Writes locally first, then mirrors to S3 best-effort."""

    name = "s3"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str = "traces",
        profile: str | None = None,
        uploader: str | None = None,
        results_dir: str | Path = "./results",
        **_ignored: Any,
    ) -> None:
        self.bucket = bucket or os.environ.get("A2A_TRACE_BUCKET")
        self.prefix = prefix
        self.profile = profile or os.environ.get("AWS_PROFILE")
        self.uploader = (
            uploader
            or os.environ.get("A2A_TRACE_USER")
            or os.environ.get("USER")
            or "unknown"
        )
        self.local = LocalJSONStore(results_dir=results_dir)

    def put_trace(self, trace: GameTraceBase, manifest: RunManifest) -> str:
        local_uri = self.local.put_trace(trace, manifest)
        trace_path = Path(local_uri)

        if not self.bucket:
            manifest.storage.backend = self.name
            manifest.storage.status = "not_configured"
            self.local.write_manifest(manifest, trace_path)
            return local_uri

        base = self._uri(
            self.prefix, self.uploader, manifest.experiment_name, manifest.experiment_run_id
        )
        trace_uri = f"{base}/{trace_path.name}"
        manifest.storage.backend = self.name
        manifest.storage.uri = trace_uri

        try:
            self._upload(trace_path, trace_uri)
            manifest.storage.status = "written"
        except Exception as exc:
            manifest.storage.status = "failed"
            manifest.storage.error = f"{type(exc).__name__}: {exc}"
            log.warning("S3 upload failed for %s: %s", manifest.experiment_run_id, exc)

        # Rewrite the local manifest so it carries the final upload status, then
        # mirror it. A manifest-upload failure must not fail the run either.
        manifest_path = self.local.write_manifest(manifest, trace_path)
        if manifest.storage.status == "written":
            try:
                self._upload(manifest_path, f"{base}/{manifest_path.name}")
            except Exception as exc:
                manifest.storage.status = "manifest_upload_failed"
                manifest.storage.error = f"{type(exc).__name__}: {exc}"
                self.local.write_manifest(manifest, trace_path)
                log.warning(
                    "S3 manifest upload failed for %s: %s", manifest.experiment_run_id, exc
                )
        # Only hand back the remote URI if the object is actually there; a failed
        # upload must resolve to the local path, which is the surviving copy.
        if manifest.storage.status == "written":
            return manifest.storage.uri or local_uri
        return local_uri

    def get_trace(self, game_id: str) -> GameTraceBase | None:
        local = self.local.get_trace(game_id)
        if local is not None or not self.bucket:
            return local
        matches = self._ls_recursive(f"{game_id}.json")
        if not matches:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / f"{game_id}.json"
            self._download(matches[0], dest)
            return GameTraceBase.model_validate_json(dest.read_text())

    def list_traces(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Listing is served from local manifests.

        The bucket is a mirror, not an index: scanning it per query would be slow
        and costly. Use the downloader scripts to hydrate local manifests first.
        """
        return self.local.list_traces(filters, limit, cursor)

    # --- preflight ---

    def check(self) -> StoreCheck:
        """Probe bucket reachability with ``head-bucket`` — a read, not a write.

        Deliberately does not upload a canary: the bucket is shared across
        collaborators and a preflight should not leave objects behind. This
        confirms the CLI is installed, credentials resolve, and the bucket is
        visible to this profile, which covers the failure modes that actually
        bite (missing profile, expired SSO, typo'd bucket).
        """
        import time as _time

        started = _time.monotonic()
        local = self.local.check()
        if not local.ok:
            return StoreCheck(
                backend=self.name, ok=False, target=str(self.local.results_dir),
                detail=f"local mirror unusable: {local.detail}",
            )
        if not self.bucket:
            return StoreCheck(
                backend=self.name, ok=False, target=None,
                detail="no bucket configured; set storage.bucket or A2A_TRACE_BUCKET",
            )
        target = f"s3://{self.bucket}/{self.prefix}"
        try:
            subprocess.run(
                self._cmd("s3api", "head-bucket", "--bucket", str(self.bucket)),
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            return StoreCheck(backend=self.name, ok=False, target=target,
                              detail="aws CLI not found on PATH")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip().splitlines()
            return StoreCheck(
                backend=self.name, ok=False, target=target,
                detail=detail[-1] if detail else f"head-bucket exited {exc.returncode}",
                latency_ms=(_time.monotonic() - started) * 1000,
            )
        return StoreCheck(
            backend=self.name, ok=True, target=target,
            detail=f"bucket reachable (profile={self.profile or 'default'})",
            latency_ms=(_time.monotonic() - started) * 1000,
        )

    # --- aws cli plumbing ---

    @staticmethod
    def _uri(*parts: str) -> str:
        return "/".join(str(p).strip("/") for p in parts if p)

    def _s3_url(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _cmd(self, *args: str) -> list[str]:
        cmd = ["aws", *args]
        if self.profile:
            cmd.extend(["--profile", self.profile])
        return cmd

    def _upload(self, local_path: Path, key: str) -> None:
        subprocess.run(
            self._cmd("s3", "cp", str(local_path), self._s3_url(key), "--only-show-errors"),
            check=True,
        )

    def _download(self, key: str, dest: Path) -> None:
        subprocess.run(
            self._cmd("s3", "cp", self._s3_url(key), str(dest), "--only-show-errors"),
            check=True,
        )

    def _ls_recursive(self, suffix: str) -> list[str]:
        result = subprocess.run(
            self._cmd(
                "s3api", "list-objects-v2",
                "--bucket", str(self.bucket),
                "--prefix", self.prefix,
                "--output", "json",
            ),
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(result.stdout or "{}")
        return [
            obj["Key"] for obj in payload.get("Contents", [])
            if obj.get("Key", "").endswith(suffix)
        ]


register_store("s3", S3TraceStore)
