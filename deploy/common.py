"""Small checked gcloud wrapper. Never print secret values."""
import json
import os
import subprocess
import time
import sys

GCLOUD = os.environ.get("GCLOUD", "gcloud")


def command(arguments, *, capture=False, stdin=None, allow_failure=False):
    result = subprocess.run([GCLOUD, *arguments], check=not allow_failure,
                            text=True, input=stdin, capture_output=capture)
    return result.stdout.strip() if capture else result.returncode


def exists(arguments):
    return subprocess.run([GCLOUD, *arguments], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def grant(project, member, role):
    command(["projects", "add-iam-policy-binding", project, "--member", member,
             "--role", role, "--condition=None", "--quiet"], capture=True)


def service_url(project, region, name="groundwork-control"):
    number = command(["projects", "describe", project, "--format=value(projectNumber)"], capture=True)
    if not number.isdigit():
        raise ValueError("Could not resolve the Google Cloud project number")
    return f"https://{name}-{number}.{region}.run.app"


def service_account(project, name):
    email = f"{name}@{project}.iam.gserviceaccount.com"
    if not exists(["iam", "service-accounts", "describe", email, "--project", project]):
        arguments = [GCLOUD, "iam", "service-accounts", "create", name, "--project", project,
                     "--display-name", name, "--quiet"]
        for attempt in range(3):
            result = subprocess.run(arguments, text=True, capture_output=True)
            if result.returncode == 0:
                break
            # Concurrent bootstrap or a completed create with an interrupted
            # response is already successful; do not create another identity.
            if exists(["iam", "service-accounts", "describe", email, "--project", project]):
                break
            exhausted = "RESOURCE_EXHAUSTED" in result.stderr or "Quota exceeded" in result.stderr
            if not exhausted or attempt == 2:
                print(result.stderr, file=sys.stderr)
                raise subprocess.CalledProcessError(result.returncode, arguments,
                                                    output=result.stdout, stderr=result.stderr)
            print(f"Service-account creation quota reached; retrying {name} in 60 seconds", file=sys.stderr)
            time.sleep(60)
    return email


def bucket(project, name, region):
    if not exists(["storage", "buckets", "describe", f"gs://{name}", "--project", project]):
        command(["storage", "buckets", "create", f"gs://{name}", "--project", project,
                 "--location", region, "--uniform-bucket-level-access", "--public-access-prevention"])


def bucket_grant(project, name, member, role):
    command(["storage", "buckets", "add-iam-policy-binding", f"gs://{name}",
             "--project", project, "--member", member, "--role", role], capture=True)
