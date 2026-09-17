"""Artifacts fail closed on identity, which is the opposite of the sink.

``event_sink`` degrades rather than raising, because losing recoverability is
bad and losing the run is worse. An artifact reference is the other posture:
bytes that do not hash to what was published are not a degraded input, they are
a different one, so reading them refuses.
"""

from __future__ import annotations

import pytest

from a2a_engine.artifacts import (
    ArtifactDigestMismatch,
    ArtifactRef,
    LocalArtifactStore,
    fetch,
    fetch_verified,
    list_artifact_stores,
    make_artifact_store,
    materialize,
    sha256_bytes,
)


def test_a_published_artifact_round_trips_under_its_own_digest(tmp_path):
    store = make_artifact_store({"backend": "local"}, root=tmp_path / "artifacts")
    ref = store.put("launches/abc/plan.yaml", b"name: plan\ncells: []\n")

    assert ref.sha256 == sha256_bytes(b"name: plan\ncells: []\n")
    assert store.get(ref.uri) == b"name: plan\ncells: []\n"
    # Resolvable without being told which store published it.
    assert fetch(ref.uri) == b"name: plan\ncells: []\n"
    assert ref.uri == store.uri("launches/abc/plan.yaml")


def test_reading_bytes_altered_after_publication_refuses(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ref = store.put("plan.yaml", b"original")
    (tmp_path / "plan.yaml").write_bytes(b"rewritten")

    with pytest.raises(ArtifactDigestMismatch):
        fetch_verified(ref)


def test_a_local_artifact_materialises_in_place_once_verified(tmp_path):
    """Returning the file itself keeps sidecar resolution -- ``presets.yaml``,
    ``storage.yaml``, the agent-pool walk-up -- identical to a direct run."""
    store = LocalArtifactStore(tmp_path)
    ref = store.put("nested/plan.yaml", b"name: plan\n")

    path = materialize(ref)
    assert path == (tmp_path / "nested" / "plan.yaml")
    assert path.read_bytes() == b"name: plan\n"


def test_materialising_into_an_explicit_destination_copies(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    ref = store.put("plan.yaml", b"name: plan\n")

    target = materialize(ref, into=tmp_path / "worker" / "plan.yaml")
    assert target.read_bytes() == b"name: plan\n"
    assert target != (tmp_path / "store" / "plan.yaml")


def test_a_key_cannot_escape_the_artifact_root(tmp_path):
    store = LocalArtifactStore(tmp_path / "root")
    with pytest.raises(ValueError):
        store.put("../outside.yaml", b"nope")


def test_an_unknown_scheme_names_what_it_knows(tmp_path):
    with pytest.raises(KeyError, match="gs"):
        fetch("gs://bucket/plan.yaml")
    assert "local" in list_artifact_stores()


def test_an_unregistered_backend_names_the_registered_ones(tmp_path):
    with pytest.raises(KeyError, match="Unknown artifact store backend"):
        make_artifact_store({"backend": "nonesuch"}, root=tmp_path)


def test_a_reference_is_just_a_uri_and_a_digest():
    ref = ArtifactRef(uri="file:///plan.yaml", sha256="abc")
    assert tuple(ref) == ("file:///plan.yaml", "abc")
