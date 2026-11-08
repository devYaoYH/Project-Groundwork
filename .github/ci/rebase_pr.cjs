// Runs only from trusted default-branch code; no PR code or git hooks execute.
const { mainHead, assertMain } = require('./pr_main.cjs');

const WORKFLOWS = new Set([
  '.github/workflows/ci.yml',
  '.github/workflows/platform.yml',
]);

module.exports = async function rebasePR({
  github, feedback, context, core, hasToken, enableRepair = false,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
}) {
  const { payload, repo } = context;
  if (!payload.issue?.pull_request || payload.comment?.body !== '/rebase') return;
  if (payload.comment.user.type !== 'User') return;

  // Association (e.g. CONTRIBUTOR) is not authorization. Check current access.
  const { data: access } = await github.rest.repos.getCollaboratorPermissionLevel({
    ...repo, username: payload.comment.user.login,
  });
  if (!['admin', 'maintain', 'write'].includes(access.permission)) {
    core.info('Ignoring /rebase from a user without repository write access.');
    return;
  }
  // Feedback must use GITHUB_TOKEN: a PAT-authored status comment creates
  // another issue_comment workflow run, even when its command job will skip.
  await feedback.rest.reactions.createForIssueComment({
    ...repo, comment_id: payload.comment.id, content: 'rocket',
  });
  core.setOutput?.('authorized', 'true');

  const pull_number = payload.issue.number;
  const runUrl = `${context.serverUrl}/${repo.owner}/${repo.repo}/actions/runs/${context.runId}`;
  const commentArgs = { ...repo, issue_number: pull_number };
  const { data: comment } = await feedback.rest.issues.createComment({
    ...commentArgs, body: `Rebasing onto \`main\` and starting PR checks. [Workflow](${runUrl})`,
  });
  core.setOutput?.('comment_id', String(comment.id));
  let stage = 'Preparing the rebase';
  let snapshot;
  let base;
  try {
    if (!hasToken) {
      throw new Error('Set the PR_WORKFLOW_TOKEN repository Actions secret first; see docs/CI.md.');
    }
    const getPR = async () => (await github.rest.pulls.get({ ...repo, pull_number })).data;
    let pr = await getPR();
    if (pr.state !== 'open') throw new Error('The pull request must be open.');
    if (pr.base.ref !== 'main') throw new Error('This command only handles PRs targeting main.');
    if (!pr.head.repo) throw new Error('The source repository has been deleted.');
    if (pr.head.repo.full_name.toLowerCase() === `${repo.owner}/${repo.repo}`.toLowerCase()
        && pr.head.ref === 'main') {
      throw new Error('Refusing to rewrite main.');
    }
    snapshot = pr;
    base = await mainHead(github, repo);
    core.setOutput?.('head', pr.head.sha);
    core.setOutput?.('base', base);

    const compare = async head => (await github.rest.repos.compareCommits({
      ...repo, base, head,
    })).data.status;
    const isCurrent = status => ['ahead', 'identical'].includes(status);
    const oldHead = pr.head.sha;
    let updated = false;
    if (!isCurrent(await compare(oldHead))) {
      await assertMain(github, repo, base);
      stage = 'Rebasing';
      // GitHub performs the rebase atomically and refuses conflicts, protected
      // branches, missing fork permissions, or a concurrent head change.
      await github.graphql(`
        mutation($id: ID!, $head: GitObjectID!) {
          updatePullRequestBranch(input: {
            pullRequestId: $id, expectedHeadOid: $head, updateMethod: REBASE
          }) { pullRequest { headRefOid } }
        }
      `, { id: pr.node_id, head: oldHead });
      for (let attempt = 0; attempt < 12; attempt++) {
        pr = await getPR();
        if (pr.head.sha !== oldHead) {
          updated = true;
          break;
        }
        await sleep(2000);
      }
      if (!updated) throw new Error('GitHub has not published a rebased head yet. Check the PR and retry.');
      if (!isCurrent(await compare(pr.head.sha))) {
        throw new Error('main or the PR changed during the rebase. Inspect the branch and retry /rebase.');
      }
    }

    stage = updated ? `Finding checks for rebased commit ${pr.head.sha}` : 'Restarting current PR checks';
    const findRuns = async () => {
      const runs = await github.paginate(github.rest.actions.listWorkflowRunsForRepo, {
        ...repo, event: 'pull_request', head_sha: pr.head.sha, per_page: 100,
      });
      // Fork runs sometimes omit pull_requests. Match immutable SHA, source
      // repository and branch too; never rerun push/manual deployment runs.
      const latest = new Map();
      for (const run of runs) {
        const path = run.path?.split('@')[0];
        if (!WORKFLOWS.has(path) || run.event !== 'pull_request'
            || run.head_sha !== pr.head.sha || run.head_branch !== pr.head.ref
            || run.head_repository?.full_name.toLowerCase() !== pr.head.repo.full_name.toLowerCase()
            || (run.pull_requests?.length && !run.pull_requests.some(p => p.number === pull_number))) continue;
        if (!latest.has(path) || run.id > latest.get(path).id) latest.set(path, run);
      }
      return latest;
    };

    let runs = await findRuns();
    // The synchronize event can take time to create the ordinary PR workflows.
    for (let attempt = 0; updated && !runs.has('.github/workflows/ci.yml') && attempt < 12; attempt++) {
      await sleep(5000);
      runs = await findRuns();
    }
    if (!runs.has('.github/workflows/ci.yml')) {
      throw new Error('No CI pull_request run was found for this head. Check Actions permissions and workflow approval; no old-commit run was restarted.');
    }
    // Ensure an intervening push/retarget/close cannot be reported as tested.
    const current = await getPR();
    if (current.head.sha !== pr.head.sha || current.base.ref !== 'main' || current.state !== 'open') {
      throw new Error('The PR changed while checks were being located. Retry /rebase on its current head.');
    }
    await assertMain(github, repo, base);
    const links = [];
    const attempts = {};
    for (const run of runs.values()) {
      let action = 'running';
      if (!updated && run.status === 'completed') {
        await github.rest.actions.reRunWorkflow({ ...repo, run_id: run.id });
        action = 'restarted';
      } else if (run.status === 'completed') {
        action = `completed: ${run.conclusion}`;
      }
      attempts[run.path.split('@')[0]] = {
        id: run.id, attempt: (run.run_attempt || 1) + (action === 'restarted' ? 1 : 0),
      };
      links.push(`- [${run.name}](${run.html_url}) — ${action}`);
    }
    core.setOutput?.('head', pr.head.sha);
    core.setOutput?.('base', base);
    core.setOutput?.('attempts', JSON.stringify(attempts));
    core.setOutput?.('repair', 'false');
    await feedback.rest.issues.updateComment({
      ...repo, comment_id: comment.id,
      body: `${updated ? 'Rebased onto' : 'Already up to date with'} \`main\`. Checks for \`${pr.head.sha}\`:\n\n${links.join('\n')}\n\nPlatform checks follow their normal path filter. Test results appear in the linked runs and the PR checks. [Command workflow](${runUrl})`,
    });
  } catch (error) {
    if (enableRepair && snapshot && stage === 'Rebasing') {
      // Only an unchanged PR can be handed to the agent. Permission errors,
      // races, and other failures are revalidated before any paid generation.
      const { data: current } = await github.rest.pulls.get({ ...repo, pull_number });
      if (current.head.sha === snapshot.head.sha && current.base.ref === 'main'
          && current.state === 'open' && current.rebaseable === false
          && await mainHead(github, repo) === base) {
        core.setOutput?.('repair', 'true');
        await feedback.rest.issues.updateComment({
          ...repo, comment_id: comment.id,
          body: `The native rebase needs conflict resolution. Starting one Codex repair attempt with \`gpt-6-luna\`. [Workflow](${runUrl})`,
        });
        return;
      }
    }
    core.setFailed(`${stage}: ${error.message}`);
    await feedback.rest.issues.updateComment({
      ...repo, comment_id: comment.id,
      body: `${stage} could not finish: ${error.message}\n\nNo conflict resolution or test-failure repair was attempted. Inspect the PR and [workflow log](${runUrl}); after resolving the blocker, comment \`/rebase\` again.`,
    });
  }
};
