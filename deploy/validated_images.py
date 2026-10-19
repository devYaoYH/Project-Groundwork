"""Publish the exact image configurations that passed validation; never rebuild."""

import argparse
import json
import re
import subprocess
from pathlib import Path

try:
    from .common import command
    from .prepare_runtime import image_is_missing
except ImportError:
    from common import command
    from prepare_runtime import image_is_missing

SOURCE = re.compile(r"[0-9a-f]{40,64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
LOCAL_IMAGES = {
    "platform": "groundwork-platform:local",
    "runtime": "groundwork-runtime:local",
}


def docker(*arguments):
    return subprocess.run(
        ["docker", *arguments], check=True, text=True, capture_output=True
    ).stdout.strip()


def require_source(value):
    if not SOURCE.fullmatch(value):
        raise ValueError("A complete source SHA is required")


def image_id(image):
    value = docker("image", "inspect", "--format", "{{.Id}}", image)
    if not DIGEST.fullmatch(value):
        raise ValueError("Docker did not return an immutable image configuration ID")
    return value


def write_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(path)


def record_images(path, source_sha, runtime_source_sha, reused_runtime=""):
    require_source(source_sha)
    require_source(runtime_source_sha)
    if reused_runtime and not DIGEST.fullmatch(reused_runtime.rpartition("@")[2]):
        raise ValueError("An existing science image must be pinned by digest")
    record = {
        "schema_version": 1,
        "source_sha": source_sha,
        "runtime_source_sha": runtime_source_sha,
        "reused_runtime": reused_runtime,
        "images": {name: image_id(tag) for name, tag in LOCAL_IMAGES.items()},
        "validated": False,
    }
    write_record(path, record)
    return record


def read_record(path, source_sha):
    require_source(source_sha)
    record = json.loads(Path(path).read_text())
    if record.get("schema_version") != 1 or record.get("source_sha") != source_sha:
        raise ValueError("Image validation belongs to a different source revision")
    require_source(record["runtime_source_sha"])
    if set(record["images"]) != set(LOCAL_IMAGES):
        raise ValueError("Both validated images are required")
    for name, tag in LOCAL_IMAGES.items():
        expected = record["images"][name]
        if not DIGEST.fullmatch(expected) or image_id(tag) != expected:
            raise ValueError(f"The {name} image changed after validation started")
    return record


def verify_images(path, source_sha):
    record = read_record(path, source_sha)
    record["validated"] = True
    write_record(path, record)
    return record


def repositories(project, region):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", project) or not re.fullmatch(
        r"[a-z][a-z0-9-]*", region
    ):
        raise ValueError("Invalid registry project or region")
    prefix = f"{region}-docker.pkg.dev/{project}"
    return {
        "platform": prefix + "/groundwork/platform",
        "runtime": prefix + "/groundwork-runtime/runtime",
    }


def check_release(
    project,
    region,
    source_sha,
    validated_source_sha,
    platform_image,
    runtime_image,
    runtime_source_sha,
):
    require_source(source_sha)
    require_source(runtime_source_sha)
    if source_sha != validated_source_sha:
        raise ValueError("Deployment source differs from the validated source")
    for name, image in (("platform", platform_image), ("runtime", runtime_image)):
        require_registry_image(image, repositories(project, region)[name])


def require_registry_image(image, expected_repository):
    repository, separator, digest = image.rpartition("@")
    if repository != expected_repository or separator != "@" or not DIGEST.fullmatch(digest):
        raise ValueError("Deployment requires an image digest in the expected registry namespace")


def registry_image(project, tag):
    try:
        raw = command(
            [
                "artifacts",
                "docker",
                "images",
                "describe",
                tag,
                "--project",
                project,
                "--format=json",
            ],
            capture=True,
        )
    except subprocess.CalledProcessError as error:
        if image_is_missing(error.stderr or ""):
            return None
        raise RuntimeError("Registry lookup failed; refusing to replace an image") from error
    info = json.loads(raw)
    digest = info.get("image_summary", info.get("imageSummary", {})).get("digest")
    if not DIGEST.fullmatch(digest or ""):
        raise ValueError("Registry did not return an immutable image digest")
    return tag.rsplit(":", 1)[0] + "@" + digest


def verify_registry_image(image, expected_id):
    manifest = json.loads(docker("manifest", "inspect", image))
    if manifest.get("config", {}).get("digest") != expected_id:
        raise ValueError("Published image differs from the image that passed validation")
    return image


def publish_image(project, tag, expected_id, *, immutable=False):
    # A concurrent release may have published this science source. Reuse it
    # only when its configuration matches the image we actually tested.
    image = registry_image(project, tag) if immutable else None
    if image is None:
        docker("tag", expected_id, tag)
        docker("push", tag)
        image = registry_image(project, tag)
        if image is None:
            raise RuntimeError("Pushed image is missing from the registry")
    return verify_registry_image(image, expected_id)


def publish_images(path, project, region, source_sha, release_id):
    record = read_record(path, source_sha)
    if record.get("validated") is not True:
        raise ValueError("Image tests must finish successfully before publication")
    if not re.fullmatch(r"[0-9]+-[0-9]+", release_id):
        raise ValueError("A workflow run ID and attempt are required")
    repos = repositories(project, region)
    reused = record["reused_runtime"]
    if reused:
        # Validate the namespace before making any registry requests or pushes.
        require_registry_image(reused, repos["runtime"])
        runtime = verify_registry_image(reused, record["images"]["runtime"])
    else:
        runtime = publish_image(
            project,
            repos["runtime"] + ":" + record["runtime_source_sha"],
            record["images"]["runtime"],
            immutable=True,
        )
    platform = publish_image(
        project,
        repos["platform"] + ":" + source_sha + "-" + release_id,
        record["images"]["platform"],
    )
    result = {
        "platform-image": platform,
        "runtime-image": runtime,
        "runtime-source-sha": record["runtime_source_sha"],
        "source-sha": source_sha,
    }
    record["published"] = result
    write_record(path, record)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("record", "verify", "publish"):
        operation = sub.add_parser(name)
        operation.add_argument("--manifest", default=".platform-build/validated-images.json")
        operation.add_argument("--source-sha", required=True)
        if name == "record":
            operation.add_argument("--runtime-source-sha", required=True)
            operation.add_argument("--reused-runtime", default="")
        if name == "publish":
            operation.add_argument("--project", required=True)
            operation.add_argument("--region", required=True)
            operation.add_argument("--release-id", required=True)
            operation.add_argument("--github-output", required=True)
    check = sub.add_parser("check")
    for name in (
        "project",
        "region",
        "source-sha",
        "validated-source-sha",
        "platform-image",
        "runtime-image",
        "runtime-source-sha",
    ):
        check.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.operation == "record":
        record_images(args.manifest, args.source_sha, args.runtime_source_sha, args.reused_runtime)
    elif args.operation == "verify":
        verify_images(args.manifest, args.source_sha)
    elif args.operation == "publish":
        result = publish_images(
            args.manifest, args.project, args.region, args.source_sha, args.release_id
        )
        with open(args.github_output, "a") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    else:
        check_release(
            args.project,
            args.region,
            args.source_sha,
            args.validated_source_sha,
            args.platform_image,
            args.runtime_image,
            args.runtime_source_sha,
        )


if __name__ == "__main__":
    main()
