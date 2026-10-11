"""Small checked gcloud wrapper. Never print secret values."""
import json
import os
import subprocess

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


def service_account(project, name):
    email = f"{name}@{project}.iam.gserviceaccount.com"
    if not exists(["iam", "service-accounts", "describe", email, "--project", project]):
        command(["iam", "service-accounts", "create", name, "--project", project,
                 "--display-name", name, "--quiet"])
    return email


def bucket(project, name, region):
    if not exists(["storage", "buckets", "describe", f"gs://{name}", "--project", project]):
        command(["storage", "buckets", "create", f"gs://{name}", "--project", project,
                 "--location", region, "--uniform-bucket-level-access", "--public-access-prevention"])


def bucket_grant(project, name, member, role):
    command(["storage", "buckets", "add-iam-policy-binding", f"gs://{name}",
             "--project", project, "--member", member, "--role", role], capture=True)
