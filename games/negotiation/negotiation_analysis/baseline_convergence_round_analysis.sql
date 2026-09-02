SELECT
  model_a,
  mc_bucket,
  convergence_round,
  COUNT(*) as N
FROM games
WHERE convergence_round > 0
  AND is_baseline = true
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY model_a, mc_bucket, convergence_round
ORDER BY model_a, mc_bucket DESC, convergence_round ASC