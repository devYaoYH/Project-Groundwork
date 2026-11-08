"""Prepare an unprivileged agent checkout and publish a validated Git bundle.

The publisher never checks out, imports, or executes the proposed source tree.
Its expected refs come from the trusted coordinator, never from the artifact.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

PROTECTED = (".github/workflows/", ".github/actions/")


def command(*args, cwd=None, check=True):
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result


def git(repository, *args, check=True):
    return command("git", "-c", "core.hooksPath=/dev/null", *args, cwd=repository, check=check)


def oid(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected a full GitHub commit SHA")
    return value


def check_paths(repository, base, result):
    paths = git(repository, "diff", "--name-only", "-z", base, result).stdout.split("\0")
    if any(name.startswith(PROTECTED) for name in paths):
        raise ValueError(
            "Agent publication cannot change workflows or local actions; review those PRs manually"
        )


def prepare(repository, head, base):
    oid(head)
    oid(base)
    if git(repository, "rev-parse", "HEAD").stdout.strip() != head:
        raise ValueError("Agent checkout differs from the authorized head")
    ancestor = git(repository, "merge-base", base, head).stdout.strip()
    check_paths(repository, ancestor, head)
    git(repository, "config", "core.hooksPath", os.devnull)
    git(repository, "config", "user.name", "groundwork-patch[bot]")
    git(repository, "config", "user.email", "groundwork-patch@users.noreply.github.com")
    git(repository, "switch", "-c", "patch-work", head)
    result = git(repository, "rebase", base, check=False)
    # Conflicts are the agent's job; other preparation errors remain failures.
    if result.returncode and not git(repository, "diff", "--name-only", "--diff-filter=U").stdout:
        raise RuntimeError(result.stderr)
    request = {"head": head, "base": base, "rebase_conflicted": bool(result.returncode)}
    (repository / ".git/patch-request.json").write_text(json.dumps(request) + "\n")
    return request


def validate_bundle(repository, bundle, base):
    oid(base)
    if bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Missing, symlinked, or oversized agent bundle")
    repository.mkdir()
    git(repository, "init", "--bare", ".")
    git(repository, "bundle", "verify", str(bundle.resolve()))
    git(repository, "fetch", str(bundle.resolve()), "refs/heads/patch-result:refs/heads/result")
    git(repository, "fsck", "--strict", "--no-dangling")
    result = oid(git(repository, "rev-parse", "refs/heads/result").stdout.strip())
    git(repository, "merge-base", "--is-ancestor", base, result)
    check_paths(repository, base, result)
    if not git(repository, "diff", "--name-only", base, result).stdout:
        raise ValueError("Refusing an agent result that removes the entire PR diff")
    return result


def pull_request(repo, number):
    return json.loads(command("gh", "api", f"repos/{repo}/pulls/{number}").stdout)


def main_head(repo):
    data = json.loads(command("gh", "api", f"repos/{repo}/git/ref/heads/main").stdout)
    return oid(data["object"]["sha"])


def validate_target(pr, repo, head, base, source_repo, source_ref, *, main):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_repo):
        raise ValueError("Invalid source repository")
    command("git", "check-ref-format", f"refs/heads/{source_ref}")
    if source_repo.lower() == repo.lower() and source_ref == "main":
        raise ValueError("Refusing to rewrite main")
    if (
        pr["state"] != "open"
        or pr["base"]["ref"] != "main"
        or main != base
        or pr["head"]["sha"] != head
        or pr["head"]["ref"] != source_ref
        or not pr["head"]["repo"]
        or pr["head"]["repo"]["full_name"].lower() != source_repo.lower()
    ):
        raise ValueError("PR or main changed since authorization; comment /rebase again")


def publish(args):
    oid(args.head)
    oid(args.base)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise ValueError("Invalid repository")
    validate_target(
        pull_request(args.repo, args.number),
        args.repo,
        args.head,
        args.base,
        args.source_repo,
        args.source_ref,
        main=main_head(args.repo),
    )
    result = validate_bundle(args.repository, args.bundle, args.base)
    if result == args.head:
        raise ValueError("Codex did not produce a new commit; no branch was changed")
    # Recheck after artifact processing. The lease also catches a later head push.
    validate_target(
        pull_request(args.repo, args.number),
        args.repo,
        args.head,
        args.base,
        args.source_repo,
        args.source_ref,
        main=main_head(args.repo),
    )
    git(
        args.repository,
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "push",
        f"https://github.com/{args.source_repo}.git",
        f"{result}:refs/heads/{args.source_ref}",
        f"--force-with-lease=refs/heads/{args.source_ref}:{args.head}",
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"head={result}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "publish"])
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--repo")
    parser.add_argument("--number", type=int)
    parser.add_argument("--source-repo")
    parser.add_argument("--source-ref")
    args = parser.parse_args()
    if args.operation == "prepare":
        request = prepare(args.repository.resolve(), args.head, args.base)
        if output_path := os.environ.get("GITHUB_OUTPUT"):
            with open(output_path, "a") as output:
                output.write(f"had_conflicts={str(request['rebase_conflicted']).lower()}\n")
    else:
        publish(args)


if __name__ == "__main__":
    main()
