"""Publish the built image's shared catalogue before a hosted revision rolls out."""
import argparse
import json
from pathlib import Path

from cloud_platform.catalogue import FirestoreCatalogueStore
from cloud_platform.storage import Repository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", type=Path, default=Path("/opt/groundwork-catalogue.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_bytes())
    document = FirestoreCatalogueStore(Repository(backend="gcp", project=args.project)).publish(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document["catalogue_sha256"] + "\n")
    print(json.dumps({"catalogue_sha256": document["catalogue_sha256"],
                      "environments": len(manifest["environments"])}))


if __name__ == "__main__":
    main()
