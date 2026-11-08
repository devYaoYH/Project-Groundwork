"""Agent output stays data; publication must preserve refs and credential boundaries."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

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
    request = patch.prepare(repo, head, base)
    assert request["rebase_conflicted"] is True
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


@pytest.mark.parametrize("concurrent_main", [False, True])
@pytest.mark.parametrize("concurrent_push", [False, True])
def test_publication_uses_a_real_lease_and_cannot_overwrite_a_racing_push(
    history, tmp_path, monkeypatch, concurrent_push, concurrent_main
):
    repo, original, head, base = history
    remote = tmp_path / "source.git"
    remote.mkdir()
    patch.git(remote, "init", "--bare", ".")
    patch.git(repo, "push", str(remote), f"{head}:refs/heads/feature", f"{base}:refs/heads/main")
    patch.prepare(repo, head, base)
    resolve(repo, monkeypatch)
    proposed = patch.git(repo, "rev-parse", "HEAD").stdout.strip()
    args = SimpleNamespace(
        repository=tmp_path / "publisher.git",
        bundle=bundle(repo, tmp_path / "result.bundle"),
        repo="owner/repo",
        number=7,
        head=head,
        base=base,
        source_repo="fork/repo",
        source_ref="feature",
    )
    pr = {
        "state": "open",
        "base": {"ref": "main", "sha": original},
        "head": {"ref": "feature", "sha": head, "repo": {"full_name": "fork/repo"}},
    }
    monkeypatch.setattr(patch, "pull_request", lambda *_args: pr)
    original_git = patch.git
    main_reads = 0

    def current_main(_repo):
        nonlocal main_reads
        main_reads += 1
        if concurrent_main and main_reads == 2:
            original_git(remote, "update-ref", "refs/heads/main", original)
        return original_git(remote, "rev-parse", "main").stdout.strip()

    monkeypatch.setattr(patch, "main_head", current_main)

    def local_git(repository, *arguments, **kwargs):
        # Route only the transport to a disposable local remote. All validation
        # and the actual force-with-lease publication use production code.
        arguments = [
            str(remote) if arg == "https://github.com/fork/repo.git" else arg for arg in arguments
        ]
        if concurrent_push and "push" in arguments:
            original_git(remote, "update-ref", "refs/heads/feature", base)
        return original_git(repository, *arguments, **kwargs)

    monkeypatch.setattr(patch, "git", local_git)
    output = tmp_path / "outputs.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if concurrent_main:
        with pytest.raises(ValueError, match="changed since authorization"):
            patch.publish(args)
        assert original_git(remote, "rev-parse", "feature").stdout.strip() == head
        assert not output.exists()
    elif concurrent_push:
        with pytest.raises(RuntimeError, match="stale info"):
            patch.publish(args)
        assert original_git(remote, "rev-parse", "feature").stdout.strip() == base
        assert not output.exists()
    else:
        patch.publish(args)
        assert original_git(remote, "rev-parse", "feature").stdout.strip() == proposed
        assert output.read_text() == f"head={proposed}\n"


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
        "main": "b" * 40,
    }
    patch.validate_target(pr, **{**args, **overrides})


@pytest.mark.parametrize("changed", ["head", "main", "closed", "base_ref", "source", "deleted"])
def test_moved_or_closed_pr_cannot_be_overwritten(changed):
    pr = deepcopy(target())
    if changed == "head":
        pr["head"]["sha"] = "c" * 40
    elif changed == "main":
        with pytest.raises(ValueError, match="changed since authorization"):
            validate(pr, main="c" * 40)
        return
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
    assert publish["env"]["GH_TOKEN"] == "${{ secrets.PR_WORKFLOW_TOKEN }}"
    for name in ("rebase", "publish", "verify"):
        checkout = next(
            step
            for step in jobs[name]["steps"]
            if step.get("uses", "").startswith("actions/checkout")
        )
        assert checkout["with"]["ref"] == "${{ github.sha }}"
        assert checkout["with"]["persist-credentials"] is False


def test_conflict_free_preparation_reports_no_conflicts(history):
    repo, original, head, _ = history
    request = patch.prepare(repo, head, original)
    assert request["rebase_conflicted"] is False


def test_final_message_and_conflict_flag_reach_both_reporting_paths_as_data():
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/pr-rebase.yml").read_text())["jobs"]
    agent = jobs["generate"]
    codex = next(step for step in agent["steps"] if step.get("id") == "codex")
    assert codex["with"]["output-file"] == "${{ runner.temp }}/codex-final-message.md"
    assert agent["outputs"]["final_message"] == "${{ steps.summary.outputs.final_message }}"
    assert agent["outputs"]["had_conflicts"] == "${{ steps.prepare.outputs.had_conflicts }}"
    summary = next(step for step in agent["steps"] if step.get("id") == "summary")
    assert summary["if"] == "always() && steps.codex.outcome != 'skipped'"
    assert "generate" in jobs["verify"]["needs"]
    for name in ("verify", "report-blocker"):
        report = jobs[name]["steps"][-1]
        assert report["env"]["CODEX_FINAL_MESSAGE"] == (
            "${{ needs.generate.outputs.final_message }}"
        )
        assert report["env"]["PATCH_HAD_CONFLICTS"] == (
            "${{ needs.generate.outputs.had_conflicts }}"
        )
        assert "process.env.CODEX_FINAL_MESSAGE" in report["with"]["script"]
        assert "${{ needs.generate.outputs.final_message }}" not in report["with"]["script"]


@pytest.mark.parametrize("token", ["workflow-token", ""])
def test_command_action_keeps_builtin_feedback_client_separate_from_pat(token):
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/pr-rebase.yml").read_text())["jobs"]
    step = next(step for step in jobs["rebase"]["steps"] if step.get("id") == "command")
    script = step["with"]["script"]
    # Execute the actual workflow wrapper with a fake Octokit constructor and
    # helper. Authentication selection must not regress to PAT-backed feedback.
    harness = r"""
      const {script, token} = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
      const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
      class Octokit { constructor(options) { this.options = options; } }
      const builtin = new Octokit({auth: 'builtin-token'});
      const helper = async options => {
        process.stdout.write(JSON.stringify({
          feedbackIsBuiltin: options.feedback === builtin,
          operationAuth: options.github.options.auth,
          hasToken: options.hasToken,
        }));
      };
      new AsyncFunction('github', 'context', 'core', 'require', 'process', script)(
        builtin, {}, {}, () => helper,
        {env: {PR_WORKFLOW_TOKEN: token, GITHUB_API_URL: 'https://api.github.com'}}
      ).catch(error => { console.error(error); process.exitCode = 1; });
    """
    result = subprocess.run(
        ["node", "-e", harness],
        input=json.dumps({"script": script, "token": token}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "feedbackIsBuiltin": True,
        "operationAuth": token or "builtin-token",
        "hasToken": bool(token),
    }


def test_comment_feedback_never_authenticates_with_the_workflow_pat():
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/pr-rebase.yml").read_text())
    jobs = workflow["jobs"]
    for name in ("rebase", "verify", "report-blocker"):
        for step in jobs[name]["steps"]:
            if step.get("uses", "").startswith("actions/github-script"):
                assert step["with"].get("github-token", "${{ github.token }}") == (
                    "${{ github.token }}"
                )
    assert jobs["rebase"]["permissions"]["issues"] == "write"
    assert jobs["rebase"]["permissions"]["actions"] == "read"
    group = workflow["concurrency"]["group"]
    assert "github.event.comment.body == '/rebase'" in group
    assert "format('pr-rebase-{0}', github.event.issue.number)" in group
    assert "format('pr-rebase-ignore-{0}', github.run_id)" in group
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_publication_validates_main_directly_despite_stale_pr_base():
    pr = target()
    pr["base"]["sha"] = "c" * 40
    validate(pr)


def test_publisher_resolves_main_from_the_branch_ref(monkeypatch):
    calls = []

    def command(*args):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps({"object": {"sha": "b" * 40}}))

    monkeypatch.setattr(patch, "command", command)
    assert patch.main_head("owner/repo") == "b" * 40
    assert calls == [("gh", "api", "repos/owner/repo/git/ref/heads/main")]


def test_clean_rebase_inherits_main_workflow_fix_without_an_extra_repair_commit(history, tmp_path):
    repo, _, head, _ = history
    patch.git(repo, "switch", "main")
    # Keep upstream edits disjoint from the PR, as with a runner-only CI fix.
    (repo / "shared.txt").write_text("original\n")
    workflow = repo / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs:\n  web:\n    runs-on: ubuntu-24.04\n")
    patch.git(repo, "add", ".")
    patch.git(repo, "commit", "-m", "Pin Playwright runner on main")
    base = patch.git(repo, "rev-parse", "HEAD").stdout.strip()
    patch.git(repo, "switch", "--detach", head)
    request = patch.prepare(repo, head, base)
    assert request["rebase_conflicted"] is False
    assert "ubuntu-24.04" in workflow.read_text()
    # Publication accepts the inherited workflow fix: the proposal itself has
    # no workflow edits, and rebasing alone has already produced a new head.
    patch.validate_bundle(
        tmp_path / "publisher.git", bundle(repo, tmp_path / "result.bundle"), base
    )
    assert patch.git(repo, "rev-parse", "HEAD").stdout.strip() != head
    assert patch.git(repo, "log", "--format=%s", f"{base}..HEAD").stdout.strip() == (
        "Original PR feature"
    )


def test_both_ci_monitors_receive_the_resolved_main_snapshot():
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/pr-rebase.yml").read_text())["jobs"]
    initial = next(step for step in jobs["rebase"]["steps"] if step.get("id") == "checks")
    verify = jobs["verify"]["steps"][-1]
    assert initial["env"]["PATCH_BASE"] == "${{ steps.command.outputs.base }}"
    assert verify["env"]["PATCH_BASE"] == "${{ needs.rebase.outputs.base }}"
    for step in (initial, verify):
        assert "base: process.env.PATCH_BASE" in step["with"]["script"]
