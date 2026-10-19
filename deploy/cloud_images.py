"""Build candidates in Cloud Build; download, validate, and promote their exact digests."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

try:
    from . import validated_images as images
    from .common import command
except ImportError:
    import validated_images as images
    from common import command

ROOT = Path(".platform-build")


def candidate_repository(project, region):
    return images.repositories(project, region)["platform"].rsplit("/", 1)[0] + "/candidate-runtime"


def prepare(project, region, source, runtime_source, build_id):
    images.require_source(source)
    images.require_source(runtime_source)
    if not re.fullmatch(r"[0-9a-f-]{36}", build_id):
        raise ValueError("A Cloud Build UUID is required")
    repos = images.repositories(project, region)
    existing = images.registry_image(project, repos["runtime"] + ":" + runtime_source)
    record = dict(
        schema_version=1,
        source_sha=source,
        runtime_source_sha=runtime_source,
        build_id=build_id,
        reused_runtime=existing or "",
    )
    targets = {}
    for name, dockerfile, repo in (
        ("platform", "Dockerfile.platform", repos["platform"]),
        ("runtime", "Dockerfile.runtime", candidate_repository(project, region)),
    ):
        if name == "runtime" and existing:
            continue
        cache = repos["platform"].rsplit("/", 1)[0] + "/build-cache:" + name
        targets[name] = {
            "context": ".",
            "dockerfile": dockerfile,
            "tags": [repo + ":candidate-" + build_id],
            "platforms": ["linux/amd64"],
            "cache-from": ["type=registry,ref=" + cache],
            "cache-to": [
                "type=registry,ref=" + cache + ",mode=max,image-manifest=true,oci-mediatypes=true"
            ],
            "output": ["type=image,push=true,oci-mediatypes=false"],
        }
    images.write_record(ROOT / "build-inputs.json", record)
    images.write_record(
        ROOT / "cloud-bake.json",
        {
            "group": {"default": {"targets": list(targets)}},
            "target": targets,
        },
    )
    command(["auth", "configure-docker", f"{region}-docker.pkg.dev", "--quiet"])


def candidates(project, region):
    record = json.loads((ROOT / "build-inputs.json").read_text())
    metadata = json.loads((ROOT / "cloud-metadata.json").read_text())
    repos = images.repositories(project, region)
    record["images"], record["configs"] = {}, {}
    for name in ("platform", "runtime"):
        if name == "runtime" and record["reused_runtime"]:
            image = record["reused_runtime"]
            manifest = json.loads(images.docker("manifest", "inspect", image))
            config = manifest["config"]["digest"]
        else:
            repo = (
                repos["platform"] if name == "platform" else candidate_repository(project, region)
            )
            digest = metadata[name]["containerimage.digest"]
            config = metadata[name]["containerimage.config.digest"]
            image = repo + "@" + digest
            images.require_registry_image(image, repo)
        if not images.DIGEST.fullmatch(config):
            raise ValueError("Build did not return an immutable image config")
        images.verify_registry_image(image, config)
        record["images"][name], record["configs"][name] = image, config
    images.write_record(ROOT / "candidates.json", record)


def check_candidates(record, project, region, source, runtime_source, build_id):
    images.require_source(source)
    images.require_source(runtime_source)
    if (
        record.get("schema_version"),
        record.get("source_sha"),
        record.get("runtime_source_sha"),
        record.get("build_id"),
    ) != (1, source, runtime_source, build_id):
        raise ValueError("Candidate build/source does not match this validation")
    if set(record["images"]) != set(images.LOCAL_IMAGES) or set(record["configs"]) != set(
        images.LOCAL_IMAGES
    ):
        raise ValueError("Both candidate images and configurations are required")
    repos = images.repositories(project, region)
    images.require_registry_image(record["images"]["platform"], repos["platform"])
    runtime_repo = (
        repos["runtime"] if record["reused_runtime"] else candidate_repository(project, region)
    )
    images.require_registry_image(record["images"]["runtime"], runtime_repo)
    if record["reused_runtime"] and record["reused_runtime"] != record["images"]["runtime"]:
        raise ValueError("The selected science runtime changed")
    for config in record["configs"].values():
        if not images.DIGEST.fullmatch(config):
            raise ValueError("An immutable image config is required")


def pull(project, region, source, runtime_source, build_id):
    location = f"gs://{project}-groundwork-build-source/ci-image-builds/{build_id}/candidates.json"
    command(["storage", "cp", location, str(ROOT / "candidates.json")])
    record = json.loads((ROOT / "candidates.json").read_text())
    check_candidates(record, project, region, source, runtime_source, build_id)
    command(["auth", "configure-docker", f"{region}-docker.pkg.dev", "--quiet"])
    for name, tag in images.LOCAL_IMAGES.items():
        subprocess.run(["docker", "pull", record["images"][name]], check=True)
        if images.image_id(record["images"][name]) != record["configs"][name]:
            raise ValueError("Downloaded image differs from the Cloud Build output")
        images.docker("tag", record["images"][name], tag)
    (ROOT / "runtime-existing").unlink(missing_ok=True)
    if record["reused_runtime"]:
        (ROOT / "runtime-existing").touch()
    images.record_images(
        ROOT / "validated-images.json", source, runtime_source, record["reused_runtime"]
    )


def handoff(project, region, source):
    record = json.loads((ROOT / "validated-images.json").read_text())
    if record.get("schema_version") != 1 or record.get("source_sha") != source:
        raise ValueError("Validation belongs to a different source revision")
    if record.get("validated") is not True:
        raise ValueError("Candidate tests have not passed")
    candidate = json.loads((ROOT / "candidates.json").read_text())
    check_candidates(
        candidate, project, region, source, record["runtime_source_sha"], candidate["build_id"]
    )
    if record["images"] != candidate["configs"]:
        raise ValueError("Validation did not test the candidate configurations")
    return {
        "_SOURCE_SHA": source,
        "_VALIDATED_SOURCE_SHA": source,
        "_PLATFORM_IMAGE": candidate["images"]["platform"],
        "_RUNTIME_IMAGE": candidate["images"]["runtime"],
        "_RUNTIME_SOURCE_SHA": record["runtime_source_sha"],
        "_PLATFORM_CONFIG_ID": record["images"]["platform"],
        "_RUNTIME_CONFIG_ID": record["images"]["runtime"],
    }


def promote(
    project,
    region,
    source,
    validated_source,
    platform,
    runtime,
    runtime_source,
    platform_config,
    runtime_config,
):
    # This runs only in the second request, after GitHub's fail-closed gate.
    images.require_source(source)
    images.require_source(runtime_source)
    if source != validated_source:
        raise ValueError("Publication source differs from the validated source")
    repos = images.repositories(project, region)
    images.require_registry_image(platform, repos["platform"])
    runtime_repo = runtime.rpartition("@")[0]
    if runtime_repo not in (repos["runtime"], candidate_repository(project, region)):
        raise ValueError("Unexpected candidate science namespace")
    images.require_registry_image(runtime, runtime_repo)
    for image, config in ((platform, platform_config), (runtime, runtime_config)):
        if not images.DIGEST.fullmatch(config):
            raise ValueError("Publication requires the tested image configurations")
    command(["auth", "configure-docker", f"{region}-docker.pkg.dev", "--quiet"])
    images.verify_registry_image(platform, platform_config)
    images.verify_registry_image(runtime, runtime_config)
    if runtime_repo != repos["runtime"]:
        final_tag = repos["runtime"] + ":" + runtime_source
        final = images.registry_image(project, final_tag)
        if final is None:
            subprocess.run(["docker", "pull", runtime], check=True)
            if images.image_id(runtime) != runtime_config:
                raise ValueError("Science image changed during download")
            # The publisher never builds; it only copies the validated manifest/layers.
            final = images.publish_image(project, final_tag, runtime_config, immutable=True)
        images.verify_registry_image(final, runtime_config)
        if final.rpartition("@")[2] != runtime.rpartition("@")[2]:
            raise ValueError("Science promotion must preserve the tested manifest digest")
        runtime = final
    images.check_release(
        project, region, source, validated_source, platform, runtime, runtime_source
    )
    (ROOT / "promoted-runtime").write_text(runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "candidates", "pull", "promote"])
    parser.add_argument("--project", default=os.environ.get("PROJECT_ID"))
    parser.add_argument("--region", default=os.environ.get("REGION", "us-central1"))
    parser.add_argument("--source-sha", default=os.environ.get("SOURCE_SHA"))
    parser.add_argument("--runtime-source-sha", default=os.environ.get("RUNTIME_SOURCE_SHA"))
    parser.add_argument("--build-id", default=os.environ.get("BUILD_ID"))
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    if args.operation == "prepare":
        prepare(args.project, args.region, args.source_sha, args.runtime_source_sha, args.build_id)
    elif args.operation == "candidates":
        candidates(args.project, args.region)
    elif args.operation == "pull":
        pull(args.project, args.region, args.source_sha, args.runtime_source_sha, args.build_id)
    else:
        promote(
            args.project,
            args.region,
            args.source_sha,
            os.environ["VALIDATED_SOURCE_SHA"],
            os.environ["PLATFORM_IMAGE"],
            os.environ["RUNTIME_IMAGE"],
            args.runtime_source_sha,
            os.environ["PLATFORM_CONFIG_ID"],
            os.environ["RUNTIME_CONFIG_ID"],
        )


if __name__ == "__main__":
    main()
