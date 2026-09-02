SELECT 
  pair,
  AVG(CASE WHEN convergence_round IS NULL THEN 0 ELSE 1 END) AS convergence_rate 
FROM games
WHERE is_baseline = false
  AND share_projects = false
  AND think_about_opponent = false
  AND pair IN (
    'claude-sonnet-4-5',
    'claude-sonnet-4-5 vs gpt-5-mini',
    'gpt-5-mini',
    'gpt-5-mini vs qwen/qwen3.5-flash-02-23',
    'qwen/qwen3.5-flash-02-23',
    'qwen/qwen3.5-flash-02-23 vs claude-sonnet-4-5'
  )
GROUP BY pair