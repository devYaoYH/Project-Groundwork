const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { monitor, prepare, latestRuns, platformPaths } = require('../pr_checks.cjs');

function fixture(options = {}) {
  const calls = { outputs: {}, failures: [], comments: [], waits: [] };
  const pr = {
    state: 'open', base: { ref: 'main', sha: 'base' },
    head: { sha: 'head', ref: 'feature', repo: { full_name: 'fork/repo' } },
    maintainer_can_modify: true,
  };
  const run = (file = 'ci', extra = {}) => ({
    id: file === 'ci' ? 10 : 11, run_attempt: 1, name: file,
    path: `.github/workflows/${file}.yml`, status: 'completed', conclusion: 'success',
    event: 'pull_request', head_sha: 'head', head_branch: 'feature',
    head_repository: { full_name: 'fork/repo' }, pull_requests: [], html_url: 'https://example.test/run',
    ...extra,
  });
  const input = {
    head: 'head', base: 'base', commentId: '23',
    context: { repo: { owner: 'owner', repo: 'repo' }, payload: { issue: { number: 7 } } },
    core: {
      setOutput(key, value) { calls.outputs[key] = value; },
      setFailed(message) { calls.failures.push(message); },
    },
    sleep: async ms => { calls.waits.push(ms); }, timeoutMs: 30000,
    paths: ['cloud_platform/**', 'web/**', 'compose.platform.yml'],
    github: {
      rest: {
        pulls: { get: async () => ({ data: pr }), listFiles: 'files' },
        actions: { listWorkflowRunsForRepo: 'runs' },
        issues: { updateComment: async args => { calls.comments.push(args.body); } },
      },
      paginate: async method => method === 'files'
        ? options.files || [{ filename: 'README.md' }]
        : options.runs ? options.runs(run) : [run()],
    },
  };
  return { calls, input, pr, run };
}

test('platform coverage is derived from the repository workflow path filter', () => {
  const text = fs.readFileSync(path.join(__dirname, '../../workflows/platform.yml'), 'utf8');
  const paths = platformPaths(text);
  assert.ok(paths.includes('cloud_platform/**'));
  assert.ok(paths.includes('.github/ci/**'));
  assert.ok(paths.includes('compose.platform.yml'));
  assert.throws(() => platformPaths('paths:\n  - new-format'), /Unsupported/);
});

test('successful CI is sufficient when platform paths are unchanged', async () => {
  const { calls, input } = fixture();
  await monitor(input);
  assert.equal(calls.outputs.repair, 'false');
  assert.deepEqual(calls.failures, []);
  assert.match(calls.comments.at(-1), /All applicable.*passed/);
});

test('platform changes require both workflows, including deleted/renamed files', async () => {
  for (const file of [
    { filename: 'web/app/page.tsx' },
    { filename: 'docs/moved.md', previous_filename: 'cloud_platform/old.py' },
    { filename: 'compose.platform.yml', status: 'removed' },
  ]) {
    const { calls, input } = fixture({ files: [file] });
    await monitor(input);
    assert.match(calls.failures[0], /Timed out/);
    assert.equal(calls.outputs.repair, undefined);
  }
});

test('current-head failure starts exactly one repair handoff with failed run IDs', async () => {
  const { calls, input } = fixture({ runs: run => [run('ci', { conclusion: 'failure' })] });
  await monitor(input);
  assert.equal(calls.outputs.repair, 'true');
  assert.equal(calls.outputs.runs, '[10]');
  assert.deepEqual(calls.failures, []);
});

test('post-repair CI failure stops without starting another agent', async () => {
  const { calls, input } = fixture({ runs: run => [run('ci', { conclusion: 'failure' })] });
  input.allowRepair = false;
  await monitor(input);
  assert.equal(calls.outputs.repair, undefined);
  assert.match(calls.failures[0], /single repair attempt/);
});

test('approval-required and cancelled runs do not launch paid code repair', async () => {
  for (const conclusion of ['action_required', 'cancelled', 'startup_failure']) {
    const { calls, input } = fixture({ runs: run => [run('ci', { conclusion })] });
    await monitor(input);
    assert.match(calls.failures[0], /require attention/);
    assert.equal(calls.outputs.repair, undefined);
  }
});

test('a completed old run attempt is not mistaken for the requested rerun', async () => {
  const { calls, input, run } = fixture();
  input.attempts = { '.github/workflows/ci.yml': { id: 10, attempt: 2 } };
  let polls = 0;
  input.github.paginate = async method => method === 'files' ? [] : [
    run('ci', { run_attempt: ++polls === 1 ? 1 : 2 }),
  ];
  await monitor(input);
  assert.equal(polls, 2);
  assert.equal(calls.outputs.repair, 'false');
});

test('a stale SHA, other fork, other branch, or deployment event never supplies test evidence', () => {
  const { pr, run } = fixture();
  for (const changes of [
    { head_sha: 'stale' }, { head_branch: 'other' },
    { head_repository: { full_name: 'other/repo' } },
    { event: 'workflow_dispatch' }, { event: 'push' },
    { pull_requests: [{ number: 99 }] },
  ]) assert.equal(latestRuns([run('ci', changes)], pr, 7).size, 0);
});

test('an intervening PR change stops monitoring and repair', async () => {
  const { calls, input, pr } = fixture();
  let reads = 0;
  input.github.rest.pulls.get = async () => ({
    data: { ...pr, head: { ...pr.head, sha: ++reads === 1 ? 'head' : 'new-head' } },
  });
  await monitor(input);
  assert.match(calls.failures[0], /PR changed/);
  assert.equal(calls.outputs.repair, undefined);
});

test('long-running checks time out instead of triggering speculative repair', async () => {
  const { calls, input } = fixture({ runs: run => [run('ci', { status: 'in_progress' })] });
  await monitor(input);
  assert.match(calls.failures[0], /Timed out/);
  assert.equal(calls.outputs.repair, undefined);
});

test('a truncated PR file listing fails closed on platform coverage', async () => {
  const { calls, input } = fixture({ files: Array(3000).fill({ filename: 'README.md' }) });
  await monitor(input);
  assert.match(calls.failures[0], /truncated/);
});

test('repair preparation requires both secrets without exposing their values', async () => {
  const { input } = fixture();
  await assert.rejects(prepare({ ...input, hasKey: false, hasToken: true }), /OPENAI_API_KEY/);
  await assert.rejects(prepare({ ...input, hasKey: true, hasToken: false }), /PR_REBASE_TOKEN/);
});

test('repair cannot start on a moved head, moved main, or a non-editable fork', async () => {
  for (const change of ['head', 'base', 'fork']) {
    const { input, pr } = fixture();
    if (change === 'head') pr.head.sha = 'changed';
    if (change === 'base') pr.base.sha = 'changed';
    if (change === 'fork') pr.maintainer_can_modify = false;
    await assert.rejects(prepare({ ...input, hasKey: true, hasToken: true, failedRuns: [] }),
      /changed|maintainer edits/);
  }
});
