"""Local release metadata, discovered at build time and read without catalogue SQL.

The JSON document is portable to a hosted catalogue publisher. Local developers
need only installed game entry points and the existing Compose build workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from a2a_engine.environment import ReleaseDeclaration
from local_stack.control_plane import ControlPlane, Release
from scripts.export_environment_ids import environment_ids
from a2a_engine.registry import get_environment_spec


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def installed_manifest(workspace):
    """Export installed declarations; fail on broken advertised entry points."""
    environments = []
    for identifier in environment_ids():
        spec = get_environment_spec(identifier)
        declaration = spec.declaration
        if declaration.environment_id != identifier:
            raise ValueError(f"Environment {identifier!r} declares a different environment ID")
        environments.append({"environment_id": identifier, "package": spec.package,
                             "declaration": declaration.model_dump(mode="json")})
    # This method scans checked-in template files; it needs only a workspace,
    # and creates no database or runtime launcher.
    reader = ControlPlane.__new__(ControlPlane)
    reader.workspace = Path(workspace).resolve()
    return {"schema_version": 1, "environments": environments,
            "experiments": reader.available_experiments()}


def declaration_from_document(document):
    # JSON represents both categorical lists and numeric interval tuples as
    # arrays. Restore intervals using the declaration's explicit parameter type.
    payload = deepcopy(document)
    for parameter in payload.get("parameters", []):
        domain = parameter.get("domain")
        if parameter.get("type") in {"continuous", "integer"} and isinstance(domain, list):
            parameter["domain"] = tuple(domain)
    return ReleaseDeclaration.model_validate(payload)


def validate_manifest(manifest):
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported catalogue schema version")
    identifiers, release_ids = set(), set()
    for entry in manifest["environments"]:
        declaration = declaration_from_document(entry["declaration"])
        identifier = entry["environment_id"]
        release_id = declaration.id or identifier
        if declaration.environment_id != identifier or identifier in identifiers or release_id in release_ids:
            raise ValueError("Catalogue environment and release identities must be unique and consistent")
        identifiers.add(identifier)
        release_ids.add(release_id)
    if not identifiers:
        raise ValueError("Catalogue has no installed environment declarations")


class LocalCatalogueStore:
    """Content-addressed JSON documents on the existing persistent local volume."""

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "catalogues"

    def publish(self, manifest):
        validate_manifest(manifest)
        digest = hashlib.sha256(encoded(manifest)).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (digest + ".json")
        document = {"catalogue_sha256": digest, "manifest": manifest,
                    "registered_at": datetime.now(UTC).isoformat()}
        # A concurrent API instance can publish the same catalogue safely.
        # Existing snapshots are retained, never replaced by a new image.
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".catalogue-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded(document))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        stored = json.loads(path.read_bytes())
        if hashlib.sha256(encoded(stored["manifest"])).hexdigest() != digest:
            raise ValueError("Persisted catalogue checksum mismatch")
        return stored


class LocalCatalogue(ControlPlane):
    """Reuse the control plane's pure read/validation methods, without a database.

    SQL-backed methods are deliberately unavailable. Experiment compilation and
    episode execution continue to use their separate private SQLite snapshots.
    """

    def __init__(self, workspace, data_dir, manifest_path=None):
        self.workspace = Path(workspace).resolve()
        if manifest_path:
            manifest = json.loads(Path(manifest_path).read_bytes())
        else:
            # Direct Python development has no image build step. Discover once
            # on first use; a process restart refreshes newly installed games.
            manifest = installed_manifest(self.workspace)
        self.document = LocalCatalogueStore(data_dir).publish(manifest)
        self.manifest = self.document["manifest"]
        self._entries = {entry["environment_id"]: entry for entry in self.manifest["environments"]}
        self._declarations = {identifier: declaration_from_document(entry["declaration"])
                              for identifier, entry in self._entries.items()}
        self._releases = {}
        self._banks = {}
        for identifier, declaration in self._declarations.items():
            release = Release(id=declaration.id or identifier, environment_id=identifier,
                version=declaration.version, declaration_sha256=declaration.content_sha256(),
                item_bank_sha256=declaration.item_policy.item_bank_sha256 if declaration.item_policy else None,
                oracle_version=declaration.oracle_version, package=self._entries[identifier].get("package"),
                source_ref="local-workspace", metadata={"launcher": "local", "entrypoint": identifier,
                    "source_url": declaration.source_url, "blurb": declaration.blurb},
                created_at=self.document["registered_at"])
            self._releases[release.id] = release

    def _session(self):
        raise RuntimeError("Read-only catalogue does not provide a SQLite session")

    def seed_installed_releases(self):
        raise RuntimeError("Catalogue declarations are already published")

    def releases(self):
        return [deepcopy(release) for release in sorted(self._releases.values(), key=lambda row: row.environment_id)]

    def environment_summaries(self):
        return [{"environment_id": identifier, "blurb": declaration.blurb,
                 "version": declaration.version, "release_id": declaration.id or identifier,
                 "source_url": declaration.source_url, "experiment_count": 0}
                for identifier, declaration in sorted(self._declarations.items())]

    def available_experiments(self):
        return deepcopy(self.manifest["experiments"])

    def _declaration(self, environment_id):
        if environment_id not in self._declarations:
            raise KeyError(f"Environment {environment_id!r} is not in this catalogue")
        return self._declarations[environment_id]

    def _item_bank(self, environment_id):
        if environment_id not in self._banks:
            self._banks[environment_id] = super()._item_bank(environment_id)
        return self._banks[environment_id]

    def _release_by_id(self, release_id):
        if release_id not in self._releases:
            raise ValueError(f"No release registered as {release_id!r}")
        return self._releases[release_id]

    def _release_for(self, environment_id, release_id):
        declaration = self._declaration(environment_id)
        release = self._release_by_id(release_id or declaration.id or environment_id)
        if release.environment_id != environment_id:
            raise ValueError(f"Release {release.id!r} belongs to another environment")
        return release
