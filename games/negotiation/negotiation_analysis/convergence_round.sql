SELECT
  mc_bucket,
  AVG(convergence_round) AS avg_convergence_rnd 
FROM games
WHERE is_baseline = false
  AND share_projects = false
  AND think_about_opponent = false
  AND convergence_round > 0
  AND pair IN (
    'claude-sonnet-4-5',
    'claude-sonnet-4-5 vs gpt-5-mini',
    'gpt-5-mini',
    'gpt-5-mini vs qwen/qwen3.5-flash-02-23',
    'qwen/qwen3.5-flash-02-23',
    'qwen/qwen3.5-flash-02-23 vs claude-sonnet-4-5'
  )
GROUP BY mc_bucket