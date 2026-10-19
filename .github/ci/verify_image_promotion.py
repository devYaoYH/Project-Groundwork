"""Exercise production image promotion against disposable Docker registry fixtures."""

import importlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=90)
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        result.check_returncode()
    return result.stdout.strip()


def main():
    sys.path.insert(0, str(ROOT))
    images = importlib.import_module("deploy.validated_images")
    fixture = uuid.uuid4().hex[:12]
    registry = "groundwork-promotion-check-" + fixture
    image_ids = []
    try:
        docker("run", "-d", "--name", registry, "-p", "127.0.0.1::5000", "registry:2")
        ports = json.loads(docker("inspect", registry))[0]["NetworkSettings"]["Ports"]
        address = "127.0.0.1:" + ports["5000/tcp"][0]["HostPort"]
        for _ in range(40):
            try:
                urlopen("http://" + address + "/v2/", timeout=2).read()
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("Disposable registry did not become ready")

        local_tags = {
            name: "groundwork-promotion-check:" + fixture + "-" + name
            for name in ("platform", "runtime")
        }
        with tempfile.TemporaryDirectory(prefix="groundwork-promotion-") as directory:
            context = Path(directory)
            (context / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
            for name, tag in local_tags.items():
                (context / "marker").write_text(fixture + "-" + name)
                docker(
                    "build",
                    "--builder",
                    docker("context", "show"),
                    "--provenance=false",
                    "--tag",
                    tag,
                    str(context),
                )
                image_ids.append(images.image_id(tag))
            images.LOCAL_IMAGES = local_tags
            images.repositories = lambda project, region: {
                name: address + "/" + name for name in local_tags
            }

            def lookup(project, tag):
                repository, version = tag.rsplit(":", 1)
                name = repository.split("/", 1)[1]
                request = Request(
                    "http://" + address + "/v2/" + name + "/manifests/" + version,
                    headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
                )
                try:
                    with urlopen(request, timeout=5) as response:
                        response.read()
                        return repository + "@" + response.headers["Docker-Content-Digest"]
                except HTTPError as error:
                    if error.code == 404:
                        return None
                    raise

            images.registry_image = lookup
            commands = []

            def registry_docker(*arguments):
                commands.append(arguments)
                if arguments[:2] == ("manifest", "inspect"):
                    arguments = (*arguments[:2], "--insecure", *arguments[2:])
                return docker(*arguments)

            images.docker = registry_docker
            manifest = context / "validated.json"
            source, science = "a" * 40, "b" * 40
            images.record_images(manifest, source, science)
            images.verify_images(manifest, source)
            result = images.publish_images(manifest, "fixture", "fixture", source, "123-1")
            assert sum(command[0] == "push" for command in commands) == 2
            assert all("@sha256:" in result[key] for key in ("platform-image", "runtime-image"))
            print("Validated image contents published and handed off by digest", flush=True)

            images.record_images(manifest, source, science, result["runtime-image"])
            images.verify_images(manifest, source)
            commands.clear()
            images.publish_images(manifest, "fixture", "fixture", source, "123-2")
            assert sum(command[0] == "push" for command in commands) == 1
            print("Existing science digest reused without another science push", flush=True)

            commands.clear()
            try:
                images.publish_image(
                    "fixture", address + "/runtime:" + science, image_ids[0], immutable=True
                )
            except ValueError as error:
                assert "differs from the image" in str(error)
            else:
                raise AssertionError("Conflicting runtime content was accepted")
            assert not any(command[0] == "push" for command in commands)
            print("Conflicting published science content rejected before push", flush=True)

            cloud = importlib.import_module("deploy.cloud_images")
            cloud.ROOT = context
            cloud.command = lambda *args, **kwargs: ""
            cloud.candidate_repository = lambda *args: address + "/candidate-runtime"
            candidate_tag = address + "/candidate-runtime:" + fixture
            docker("tag", image_ids[1], candidate_tag)
            docker("push", candidate_tag)
            candidate = lookup("fixture", candidate_tag)
            commands.clear()
            cloud.promote(
                "fixture",
                "fixture",
                source,
                source,
                result["platform-image"],
                candidate,
                "c" * 40,
                image_ids[0],
                image_ids[1],
            )
            promoted = (context / "promoted-runtime").read_text()
            assert promoted.rpartition("@")[2] == candidate.rpartition("@")[2]
            assert sum(command[0] == "push" for command in commands) == 1
            commands.clear()
            cloud.promote(
                "fixture",
                "fixture",
                source,
                source,
                result["platform-image"],
                promoted,
                "c" * 40,
                image_ids[0],
                image_ids[1],
            )
            assert not any(command[0] == "push" for command in commands)
            print(
                "Cloud candidate promoted with the identical manifest digest; old science reused",
                flush=True,
            )

    finally:
        subprocess.run(["docker", "rm", "-f", registry], capture_output=True, timeout=30)
        if image_ids:
            subprocess.run(
                ["docker", "image", "rm", "--force", *image_ids], capture_output=True, timeout=30
            )


if __name__ == "__main__":
    main()
