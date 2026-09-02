WITH baseline AS (
SELECT
  model_a,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
  COUNT(*) AS baseline_N
FROM rounds
WHERE is_baseline = true
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY model_a, is_shifting, mc_bucket
),
data_tbl AS (
SELECT
  model_a,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
  COUNT(*) as N
FROM rounds
WHERE is_baseline = false
  AND is_cross_play = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY model_a, is_shifting, mc_bucket
)
SELECT
  CASE WHEN data_tbl.is_shifting THEN data_tbl.model_a || ' (shifting)' ELSE data_tbl.model_a END AS model_a,
  data_tbl.mc_bucket,
  ROUND(data_tbl.avg_overdrawn * 1000) / 10 AS ovr_pct,
  ROUND(baseline.avg_overdrawn * 1000) / 10 AS baseline_ovr_pct,
  ROUND(data_tbl.avg_eff * 1000) / 10 AS joint_eff,
  ROUND(baseline.avg_eff * 1000) / 10 AS baseline_joint_eff,
  ROUND(data_tbl.avg_opt * 1000) / 10 AS opt_rate,
  ROUND(baseline.avg_opt * 1000) / 10 AS baseline_opt_rate,
  data_tbl.N,
  baseline.baseline_N
FROM data_tbl
JOIN baseline ON (data_tbl.model_a = baseline.model_a AND data_tbl.is_shifting = baseline.is_shifting AND data_tbl.mc_bucket = baseline.mc_bucket)
ORDER BY data_tbl.model_a, data_tbl.is_shifting ASC, data_tbl.mc_bucket DESC;
