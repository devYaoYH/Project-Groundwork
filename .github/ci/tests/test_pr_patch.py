"""Agent output stays data; publication must preserve refs and credential boundaries."""

from copy import deepcopy
from pathlib import Path

import pr_patch as patch
import pytest
import yaml


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "candidate"
    repo.mkdir()
    patch.git(repo, "init", "-b", "main")
    patch.git(repo, "config", "user.name", "Test")
    patch.git(repo, "config", "user.email", "test@example.test")
    (repo / "shared.txt").write_text("original\n")
    patch.git(repo, "add", ".")
    patch.git(repo, "commit", "-m", "Initial")
    original = patch.git(repo, "rev-parse", "HEAD").stdout.strip()
    patch.git(repo, "switch", "-c", "feature")
    (repo / "shared.txt").write_text("PR intent\n")
    (repo / "feature.txt").write_text("Keep this original PR feature\n")
    patch.git(repo, "add", ".")
    patch.git(repo, "commit", "-m", "Original PR feature")
    head = patch.git(repo, "rev-parse", "HEAD").stdout.strip()
    patch.git(repo, "switch", "main")
    (repo / "shared.txt").write_text("New main behavior\n")
    patch.git(repo, "commit", "-am", "Main advanced")
    base = patch.git(repo, "rev-parse", "HEAD").stdout.strip()
    patch.git(repo, "switch", "--detach", head)
    return repo, original, head, base


def bundle(repo, target):
    patch.git(repo, "update-ref", "refs/heads/patch-result", "HEAD")
    patch.git(repo, "bundle", "create", str(target), "refs/heads/patch-result")
    return target


def resolve(repo, monkeypatch):
    (repo / "shared.txt").write_text("New main behavior\nPR intent\n")
    patch.git(repo, "add", "shared.txt")
    monkeypatch.setenv("GIT_EDITOR", "true")
    patch.git(repo, "rebase", "--continue")


def test_real_conflicting_rebase_can_be_resolved_bundled_and_validated(
    history, tmp_path, monkeypatch
):
    repo, _, head, base = history
    patch.prepare(repo, head, base)
    assert (repo / ".git/rebase-merge").is_dir()
    assert "<<<<<<<" in (repo / "shared.txt").read_text()
    resolve(repo, monkeypatch)
    result = patch.validate_bundle(
        tmp_path / "publisher.git", bundle(repo, tmp_path / "result.bundle"), base
    )
    assert result != head
    assert (repo / "feature.txt").read_text() == "Keep this original PR feature\n"
    assert patch.git(repo, "merge-base", "--is-ancestor", base, result).returncode == 0
    assert patch.git(repo, "log", "--format=%s", f"{base}..{result}").stdout.strip() == (
        "Original PR feature"
    )
    # Publisher is bare: candidate source, hooks, and imports are never executed.
    assert not (tmp_path / "publisher.git/shared.txt").exists()


def test_checkout_must_match_authorized_head(history):
    repo, original, _, base = history
    with pytest.raises(ValueError, match="authorized head"):
        patch.prepare(repo, original, base)


def test_workflow_changing_pr_is_rejected_before_paid_generation(history):
    repo, _, _, base = history
    workflow = repo / ".github/workflows/unsafe.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("do not execute me\n")
    patch.git(repo, "add", ".")
    patch.git(repo, "commit", "-m", "Change automation")
    head = patch.git(repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(ValueError, match="cannot change workflows"):
        patch.prepare(repo, head, base)


def test_agent_cannot_add_workflow_edits_to_a_normal_pr(history, tmp_path, monkeypatch):
    repo, _, head, base = history
    patch.prepare(repo, head, base)
    resolve(repo, monkeypatch)
    workflow = repo / ".github/workflows/unsafe.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("do not execute me\n")
    patch.git(repo, "add", ".github")
    patch.git(repo, "commit", "-m", "Unauthorized automation edit")
    with pytest.raises(ValueError, match="cannot change workflows"):
        patch.validate_bundle(
            tmp_path / "publisher.git", bundle(repo, tmp_path / "result.bundle"), base
        )


def test_a_bundle_not_rebased_onto_authorized_main_is_rejected(history, tmp_path):
    repo, _, _, base = history
    with pytest.raises(RuntimeError, match="git failed"):
        patch.validate_bundle(
            tmp_path / "publisher.git", bundle(repo, tmp_path / "result.bundle"), base
        )


def test_an_empty_pr_result_is_rejected(history, tmp_path):
    repo, _, _, base = history
    patch.git(repo, "switch", "--detach", base)
    with pytest.raises(ValueError, match="removes the entire PR diff"):
        patch.validate_bundle(
            tmp_path / "publisher.git", bundle(repo, tmp_path / "result.bundle"), base
        )


def test_symlinked_artifact_is_rejected(history, tmp_path):
    repo, _, _, base = history
    artifact = bundle(repo, tmp_path / "result.bundle")
    link = tmp_path / "link.bundle"
    link.symlink_to(artifact)
    with pytest.raises(ValueError, match="symlinked"):
        patch.validate_bundle(tmp_path / "publisher.git", link, base)


def target():
    return {
        "state": "open",
        "base": {"ref": "main", "sha": "b" * 40},
        "head": {"ref": "feature", "sha": "a" * 40, "repo": {"full_name": "fork/repo"}},
    }


def validate(pr, **overrides):
    args = {
        "repo": "owner/repo",
        "head": "a" * 40,
        "base": "b" * 40,
        "source_repo": "fork/repo",
        "source_ref": "feature",
    }
    patch.validate_target(pr, **{**args, **overrides})


@pytest.mark.parametrize("changed", ["head", "main", "closed", "base_ref", "source", "deleted"])
def test_moved_or_closed_pr_cannot_be_overwritten(changed):
    pr = deepcopy(target())
    if changed == "head":
        pr["head"]["sha"] = "c" * 40
    elif changed == "main":
        pr["base"]["sha"] = "c" * 40
    elif changed == "closed":
        pr["state"] = "closed"
    elif changed == "base_ref":
        pr["base"]["ref"] = "release"
    elif changed == "source":
        pr["head"]["repo"]["full_name"] = "different/repo"
    else:
        pr["head"]["repo"] = None
    with pytest.raises(ValueError, match="changed since authorization"):
        validate(pr)


def test_main_can_never_be_a_publication_target():
    with pytest.raises(ValueError, match="rewrite main"):
        validate(target(), source_repo="owner/repo", source_ref="main")


@pytest.mark.parametrize("value", ["short", "-bad-flag", "HEAD; echo bad", "a" * 39])
def test_commit_refs_must_be_immutable_shas(value):
    with pytest.raises(ValueError, match="commit SHA"):
        patch.oid(value)


def test_workflow_separates_generation_key_from_git_write_token():
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/pr-rebase.yml").read_text())["jobs"]
    agent = jobs["generate"]
    assert agent["permissions"] == {"contents": "read", "actions": "read"}
    codex = next(step for step in agent["steps"] if step.get("uses", "").startswith("openai/"))
    assert codex["uses"] == "openai/codex-action@bdf19a4a223ec2549a3e2274a0cf61556bc07675"
    assert codex["with"]["model"] == "gpt-6-luna"
    assert codex["with"]["openai-api-key"] == "${{ secrets.OPENAI_API_KEY }}"
    assert codex["with"]["safety-strategy"] == "drop-sudo"
    assert "secrets.OPENAI_API_KEY" not in str(jobs["publish"])
    assert "GH_TOKEN" not in str(agent)
    publish = next(step for step in jobs["publish"]["steps"] if step.get("id") == "publish")
    assert publish["env"]["GH_TOKEN"] == "${{ secrets.PR_REBASE_TOKEN }}"
    for name in ("rebase", "publish", "verify"):
        checkout = next(
            step
            for step in jobs[name]["steps"]
            if step.get("uses", "").startswith("actions/checkout")
        )
        assert checkout["with"]["ref"] == "${{ github.sha }}"
        assert checkout["with"]["persist-credentials"] is False
