// Pull request base.sha can be stale. Resolve the actual target branch instead.
async function mainHead(github, repo) {
  const { data } = await github.rest.git.getRef({ ...repo, ref: 'heads/main' });
  return data.object.sha;
}

async function assertMain(github, repo, base) {
  if (await mainHead(github, repo) !== base) {
    throw new Error('main changed since authorization; comment /rebase again.');
  }
}

module.exports = { mainHead, assertMain };
