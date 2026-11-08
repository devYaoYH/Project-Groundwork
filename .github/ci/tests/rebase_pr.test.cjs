const { test } = require('node:test');
const assert = require('node:assert/strict');
const rebasePR = require('../rebase_pr.cjs');

function fixture(options = {}) {
  const calls = { mutations: [], reruns: [], comments: [], failures: [], waits: [] };
  const pr = {
    state: 'open', node_id: 'PR_node',
    base: { ref: 'main', sha: 'base' },
    head: { sha: 'old', ref: 'feature', repo: { full_name: 'contributor/repo' } },
    ...options.pr,
  };
  let mutated = false;
  const context = {
    repo: { owner: 'owner', repo: 'repo' }, serverUrl: 'https://github.com', runId: 123,
    payload: {
      issue: { number: 7, pull_request: {} },
      comment: { body: '/rebase', user: { login: 'maintainer', type: 'User' } },
    },
  };
  const run = (id, path, extra = {}) => ({
    id, path: `.github/workflows/${path}.yml`, name: path,
    event: 'pull_request', head_sha: mutated ? 'new' : 'old',
    head_branch: 'feature', head_repository: { full_name: 'contributor/repo' },
    pull_requests: [], status: 'completed', conclusion: 'success',
    html_url: `https://github.com/owner/repo/actions/runs/${id}`, ...extra,
  });
  const github = {
    rest: {
      repos: {
        getCollaboratorPermissionLevel: async () => ({ data: { permission: options.permission || 'write' } }),
        compareCommits: async () => ({ data: { status: options.behind && !mutated ? 'diverged' : 'ahead' } }),
      },
      pulls: {
        get: async () => ({
          data: { ...pr, head: pr.head && { ...pr.head, sha: mutated ? 'new' : pr.head.sha } },
        }),
      },
      issues: {
        createComment: async args => { calls.comments.push(args); return { data: { id: 456 } }; },
        updateComment: async args => { calls.comments.push(args); },
      },
      actions: {
        listWorkflowRunsForRepo: 'list-runs',
        reRunWorkflow: async args => { calls.reruns.push(args.run_id); },
      },
    },
    graphql: async (query, args) => {
      calls.mutations.push({ query, args });
      if (options.mutationError) throw new Error(options.mutationError);
      mutated = true;
      return { updatePullRequestBranch: { pullRequest: { headRefOid: 'new' } } };
    },
    paginate: async (_method, args) => {
      assert.equal(args.event, 'pull_request');
      assert.equal(args.head_sha, mutated ? 'new' : pr.head.sha);
      return options.runs ? options.runs(run) : [run(10, 'ci'), run(11, 'platform')];
    },
  };
  const input = {
    github, context, hasToken: true,
    core: { info() {}, setFailed(message) { calls.failures.push(message); } },
    sleep: async ms => { calls.waits.push(ms); },
  };
  return { calls, input, pr, context, github };
}

test('an up-to-date fork PR reruns both complete test workflows at the current SHA', async () => {
  const { calls, input } = fixture();
  await rebasePR(input);
  assert.deepEqual(calls.reruns, [10, 11]);
  assert.equal(calls.mutations.length, 0);
  assert.equal(calls.failures.length, 0);
  assert.match(calls.comments.at(-1).body, /Already up to date.*`main`/);
  assert.match(calls.comments.at(-1).body, /`old`/);
});

test('a behind fork is rebased with an expected-head guard, then new CI runs are linked', async () => {
  const { calls, input } = fixture({ behind: true });
  await rebasePR(input);
  assert.deepEqual(calls.mutations[0].args, { id: 'PR_node', head: 'old' });
  assert.match(calls.mutations[0].query, /updateMethod: REBASE/);
  assert.match(calls.mutations[0].query, /expectedHeadOid: \$head/);
  assert.deepEqual(calls.reruns, []);
  assert.equal(calls.failures.length, 0);
  assert.match(calls.comments.at(-1).body, /Rebased onto `main`.*`new`/s);
});

for (const permission of ['read', 'triage', 'none']) {
  test(`a ${permission} commenter cannot trigger writes`, async () => {
    const { calls, input } = fixture({ permission });
    await rebasePR(input);
    assert.deepEqual(calls.comments, []);
    assert.deepEqual(calls.mutations, []);
    assert.deepEqual(calls.reruns, []);
  });
}

for (const body of ['/rebase please', 'hello /rebase', '/rebase\necho hacked', ' /rebase']) {
  test(`only the exact command is accepted: ${JSON.stringify(body)}`, async () => {
    const { calls, input, context } = fixture();
    context.payload.comment.body = body;
    await rebasePR(input);
    assert.deepEqual(calls.comments, []);
  });
}

test('ordinary issues and bot comments do not trigger the command', async () => {
  for (const kind of ['issue', 'bot']) {
    const { calls, input, context } = fixture();
    if (kind === 'issue') delete context.payload.issue.pull_request;
    else context.payload.comment.user.type = 'Bot';
    await rebasePR(input);
    assert.deepEqual(calls.comments, []);
  }
});

test('missing credentials fail with setup instructions before modifying a branch', async () => {
  const { calls, input } = fixture();
  input.hasToken = false;
  await rebasePR(input);
  assert.match(calls.failures[0], /PR_REBASE_TOKEN/);
  assert.deepEqual(calls.mutations, []);
  assert.deepEqual(calls.reruns, []);
});

for (const pr of [
  { state: 'closed' },
  { base: { ref: 'release', sha: 'base' } },
  { head: { ref: 'feature', sha: 'old', repo: null } },
  { head: { ref: 'main', sha: 'old', repo: { full_name: 'owner/repo' } } },
]) {
  test(`invalid PR fails without branch mutation: ${JSON.stringify(pr)}`, async () => {
    const { calls, input } = fixture({ pr, behind: true });
    await rebasePR(input);
    assert.equal(calls.failures.length, 1);
    assert.deepEqual(calls.mutations, []);
    assert.deepEqual(calls.reruns, []);
  });
}

for (const reason of ['Rebase conflicts', 'Head changed', 'Resource not accessible', 'Branch protected']) {
  test(`GitHub refuses ${reason} without falling back to merge or unsafe push`, async () => {
    const { calls, input } = fixture({ behind: true, mutationError: reason });
    await rebasePR(input);
    assert.equal(calls.mutations.length, 1);
    assert.deepEqual(calls.reruns, []);
    assert.match(calls.failures[0], new RegExp(reason));
    assert.match(calls.comments.at(-1).body, /could not finish/);
  });
}

test('only the latest matching PR test runs can be restarted, never release/deployment runs', async () => {
  const { calls, input } = fixture({
    runs: run => [
      run(1, 'ci'), run(10, 'ci'), run(11, 'platform'),
      run(12, 'platform', { event: 'push' }),
      run(13, 'platform', { event: 'workflow_dispatch' }),
      run(14, 'ci', { head_sha: 'stale' }),
      run(15, 'ci', { head_branch: 'another-branch' }),
      run(16, 'ci', { head_repository: { full_name: 'another/repo' } }),
      run(17, 'ci', { pull_requests: [{ number: 99 }] }),
      run(18, 'other-workflow'),
    ],
  });
  await rebasePR(input);
  assert.deepEqual(calls.reruns, [10, 11]);
});

test('an in-progress run is linked rather than restarted', async () => {
  const { calls, input } = fixture({ runs: run => [run(10, 'ci', { status: 'in_progress' })] });
  await rebasePR(input);
  assert.deepEqual(calls.reruns, []);
  assert.match(calls.comments.at(-1).body, /running/);
});

test('a path-filtered platform workflow is optional', async () => {
  const { calls, input } = fixture({ runs: run => [run(10, 'ci')] });
  await rebasePR(input);
  assert.deepEqual(calls.reruns, [10]);
  assert.equal(calls.failures.length, 0);
});

test('no current-head CI produces an actionable failure, not a stale rerun', async () => {
  const { calls, input } = fixture({ runs: run => [run(10, 'ci', { head_sha: 'stale' })] });
  await rebasePR(input);
  assert.match(calls.failures[0], /No CI pull_request run/);
  assert.deepEqual(calls.reruns, []);
});

test('workflow creation after a rebase is polled', async () => {
  const { calls, input, github } = fixture({ behind: true });
  const paginate = github.paginate;
  let polls = 0;
  github.paginate = async (...args) => ++polls < 3 ? [] : paginate(...args);
  await rebasePR(input);
  assert.equal(polls, 3);
  assert.deepEqual(calls.waits, [5000, 5000]);
  assert.equal(calls.failures.length, 0);
});

test('a concurrent PR head change is not reported as tested', async () => {
  const { calls, input, github, pr } = fixture();
  let reads = 0;
  github.rest.pulls.get = async () => ({
    data: { ...pr, head: { ...pr.head, sha: ++reads === 1 ? 'old' : 'other' } },
  });
  await rebasePR(input);
  assert.match(calls.failures[0], /PR changed/);
  assert.deepEqual(calls.reruns, []);
});

test('a rerun API failure is reported instead of claiming success', async () => {
  const { calls, input, github } = fixture();
  github.rest.actions.reRunWorkflow = async () => { throw new Error('Actions write permission required'); };
  await rebasePR(input);
  assert.match(calls.failures[0], /Actions write permission/);
  assert.match(calls.comments.at(-1).body, /could not finish/);
});

test('conflicting rebase hands an unchanged head to Codex only when repair is enabled', async () => {
  const { calls, input, pr } = fixture({ behind: true, mutationError: 'Rebase conflicts' });
  pr.rebaseable = false;
  const outputs = {};
  input.core.setOutput = (key, value) => { outputs[key] = value; };
  input.enableRepair = true;
  await rebasePR(input);
  assert.equal(outputs.repair, 'true');
  assert.equal(outputs.head, 'old');
  assert.equal(outputs.base, 'base');
  assert.equal(calls.failures.length, 0);
  assert.match(calls.comments.at(-1).body, /gpt-6-luna/);
});

test('a rebase API permission error does not launch Codex for a rebaseable PR', async () => {
  const { calls, input, pr } = fixture({ behind: true, mutationError: 'Resource not accessible' });
  pr.rebaseable = true;
  input.enableRepair = true;
  await rebasePR(input);
  assert.match(calls.failures[0], /Resource not accessible/);
});
