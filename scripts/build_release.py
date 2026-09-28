#!/usr/bin/env python3
"""Build a pinned local worker image and publish its design-only manifest."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from a2a_engine.registry import discover_environments, get_environment_spec
from a2a_engine.release_surface import publish_release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path, help="games/<game>/runtime/release.json")
    parser.add_argument("--database", type=Path, help="optional control-plane database to register into")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    config = json.loads(args.release.read_text(encoding="utf-8"))
    environment_id = str(config["environment_id"])
    discover_environments()
    spec = get_environment_spec(environment_id)
    declaration = spec.declaration
    if declaration is None:
        raise ValueError(f"{environment_id} has no release declaration")
    dockerfile = args.release.parent / "Dockerfile"
    tag = f"a2a-comm-{environment_id}:local"
    subprocess.run(["docker", "build", "-f", str(dockerfile), "-t", tag, str(root)], check=True)
    digest = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], text=True).strip()
    check = (
        "from a2a_engine.registry import discover_environments,get_environment_spec; "
        "from a2a_engine.adapters import resolve_scripted_binding; "
        "import sys; discover_environments(); s=get_environment_spec(sys.argv[1]); "
        "d=s.declaration; assert d is not None; "
        "assert all(resolve_scripted_binding(r.id,b,d) in getattr(s.cls,'SCRIPTED_CLIENT_TYPES',set()) "
        "for r in d.roles for b in r.scripted_bindings)"
    )
    subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", digest,
                    "-c", check, environment_id], check=True)
    manifest = publish_release(declaration, package=spec.package, image_digest=digest)
    output = args.release.parent / "release.manifest.json"
    output.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if args.database:
        from local_stack.control_plane import ControlPlane
        ControlPlane(args.database, workspace=root).ingest_release(manifest.model_dump(mode="json"))
    print(f"{environment_id}: {digest} -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
