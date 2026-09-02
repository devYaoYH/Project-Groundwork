WITH baseline AS (
SELECT
  model_a,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt
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
FROM rounds
WHERE is_baseline = false
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
  ROUND(baseline.avg_opt * 1000) / 10 AS baseline_opt_rate
FROM data_tbl
JOIN baseline ON (data_tbl.model_a = baseline.model_a AND data_tbl.is_shifting = baseline.is_shifting AND data_tbl.mc_bucket = baseline.mc_bucket)
ORDER BY data_tbl.model_a, data_tbl.is_shifting ASC, data_tbl.mc_bucket DESC;

-- Delta query: non-shifting minus shifting
WITH baseline AS (
SELECT
  model_a,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt
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
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY model_a, is_shifting, mc_bucket
),
baseline_delta AS (
SELECT
  non_shift.model_a,
  non_shift.mc_bucket,
  (non_shift.avg_overdrawn - shift.avg_overdrawn) AS delta_overdrawn,
  (non_shift.avg_eff - shift.avg_eff) AS delta_eff,
  (non_shift.avg_opt - shift.avg_opt) AS delta_opt
FROM baseline non_shift
JOIN baseline shift
  ON non_shift.model_a = shift.model_a AND non_shift.mc_bucket = shift.mc_bucket
  AND non_shift.is_shifting = false AND shift.is_shifting = true
),
data_delta AS (
SELECT
  non_shift.model_a,
  non_shift.mc_bucket,
  (non_shift.avg_overdrawn - shift.avg_overdrawn) AS delta_overdrawn,
  (non_shift.avg_eff - shift.avg_eff) AS delta_eff,
  (non_shift.avg_opt - shift.avg_opt) AS delta_opt
FROM data_tbl non_shift
JOIN data_tbl shift
  ON non_shift.model_a = shift.model_a AND non_shift.mc_bucket = shift.mc_bucket
  AND non_shift.is_shifting = false AND shift.is_shifting = true
)
SELECT
  data_delta.model_a,
  data_delta.mc_bucket,
  ROUND(data_delta.delta_overdrawn * 1000) / 10 AS delta_ovr_pct,
  ROUND(baseline_delta.delta_overdrawn * 1000) / 10 AS baseline_delta_ovr_pct,
  ROUND(data_delta.delta_eff * 1000) / 10 AS delta_joint_eff,
  ROUND(baseline_delta.delta_eff * 1000) / 10 AS baseline_delta_joint_eff,
  ROUND(data_delta.delta_opt * 1000) / 10 AS delta_opt_rate,
  ROUND(baseline_delta.delta_opt * 1000) / 10 AS baseline_delta_opt_rate
FROM data_delta
JOIN baseline_delta ON (data_delta.model_a = baseline_delta.model_a AND data_delta.mc_bucket = baseline_delta.mc_bucket)
ORDER BY data_delta.model_a, data_delta.mc_bucket DESC;

-- Pair-based query
WITH data_tbl AS (
SELECT
  pair,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY pair, mc_bucket
)
SELECT
  data_tbl.pair,
  data_tbl.mc_bucket,
  ROUND(data_tbl.avg_overdrawn * 1000) / 10 AS ovr_pct,
  ROUND(data_tbl.avg_eff * 1000) / 10 AS joint_eff,
  ROUND(data_tbl.avg_opt * 1000) / 10 AS opt_rate
FROM data_tbl
ORDER BY data_tbl.pair, data_tbl.mc_bucket DESC;

-- Pair-based query: with is_shifting breakdown
WITH data_tbl AS (
SELECT
  pair,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY pair, is_shifting, mc_bucket
)
SELECT
  CASE WHEN data_tbl.is_shifting THEN data_tbl.pair || ' (shifting)' ELSE data_tbl.pair END AS pair,
  data_tbl.mc_bucket,
  ROUND(data_tbl.avg_overdrawn * 1000) / 10 AS ovr_pct,
  ROUND(data_tbl.avg_eff * 1000) / 10 AS joint_eff,
  ROUND(data_tbl.avg_opt * 1000) / 10 AS opt_rate
FROM data_tbl
ORDER BY data_tbl.pair, data_tbl.is_shifting ASC, data_tbl.mc_bucket DESC;

-- Pair-based delta query: non-shifting minus shifting
WITH data_tbl AS (
SELECT
  pair,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY pair, is_shifting, mc_bucket
),
data_delta AS (
SELECT
  non_shift.pair,
  non_shift.mc_bucket,
  (non_shift.avg_overdrawn - shift.avg_overdrawn) AS delta_overdrawn,
  (non_shift.avg_eff - shift.avg_eff) AS delta_eff,
  (non_shift.avg_opt - shift.avg_opt) AS delta_opt
FROM data_tbl non_shift
JOIN data_tbl shift
  ON non_shift.pair = shift.pair AND non_shift.mc_bucket = shift.mc_bucket
  AND non_shift.is_shifting = false AND shift.is_shifting = true
)
SELECT
  data_delta.pair,
  data_delta.mc_bucket,
  ROUND(data_delta.delta_overdrawn * 1000) / 10 AS delta_ovr_pct,
  ROUND(data_delta.delta_eff * 1000) / 10 AS delta_joint_eff,
  ROUND(data_delta.delta_opt * 1000) / 10 AS delta_opt_rate
FROM data_delta
ORDER BY data_delta.pair, data_delta.mc_bucket DESC;

-- Heterogeneous vs Homogeneous pair comparison
WITH data_tbl AS (
SELECT
  CASE WHEN model_a = model_b THEN 'homogeneous' ELSE 'heterogeneous' END AS pair_type,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY pair_type, is_shifting, mc_bucket
)
SELECT
  CASE WHEN data_tbl.is_shifting THEN data_tbl.pair_type || ' (shifting)' ELSE data_tbl.pair_type END AS pair_type,
  data_tbl.mc_bucket,
  ROUND(data_tbl.avg_overdrawn * 1000) / 10 AS ovr_pct,
  ROUND(data_tbl.avg_eff * 1000) / 10 AS joint_eff,
  ROUND(data_tbl.avg_opt * 1000) / 10 AS opt_rate
FROM data_tbl
ORDER BY data_tbl.pair_type, data_tbl.is_shifting ASC, data_tbl.mc_bucket DESC;

-- Heterogeneous vs Homogeneous delta: non-shifting minus shifting
WITH data_tbl AS (
SELECT
  CASE WHEN model_a = model_b THEN 'homogeneous' ELSE 'heterogeneous' END AS pair_type,
  is_shifting,
  mc_bucket,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
GROUP BY pair_type, is_shifting, mc_bucket
),
data_delta AS (
SELECT
  non_shift.pair_type,
  non_shift.mc_bucket,
  (non_shift.avg_overdrawn - shift.avg_overdrawn) AS delta_overdrawn,
  (non_shift.avg_eff - shift.avg_eff) AS delta_eff,
  (non_shift.avg_opt - shift.avg_opt) AS delta_opt
FROM data_tbl non_shift
JOIN data_tbl shift
  ON non_shift.pair_type = shift.pair_type AND non_shift.mc_bucket = shift.mc_bucket
  AND non_shift.is_shifting = false AND shift.is_shifting = true
)
SELECT
  data_delta.pair_type,
  data_delta.mc_bucket,
  ROUND(data_delta.delta_overdrawn * 1000) / 10 AS delta_ovr_pct,
  ROUND(data_delta.delta_eff * 1000) / 10 AS delta_joint_eff,
  ROUND(data_delta.delta_opt * 1000) / 10 AS delta_opt_rate
FROM data_delta
ORDER BY data_delta.pair_type, data_delta.mc_bucket DESC;

-- INTERVENTION: project sharing
SELECT
  model,
  share_projects,
  avg(fair_efficiency) AS fair_eff
FROM agents
WHERE mc_bucket = 0.5
  AND pair = 'qwen/qwen3.5-flash-02-23 vs claude-sonnet-4-5'
GROUP BY model, share_projects;

WITH data_tbl AS (
SELECT
  pair,
  mc_bucket,
  share_projects,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
  AND pair = 'qwen/qwen3.5-flash-02-23 vs claude-sonnet-4-5'
  AND mc_bucket = 0.5
GROUP BY pair, mc_bucket, share_projects
)
SELECT
  data_tbl.pair,
  data_tbl.mc_bucket,
  data_tbl.share_projects,
  ROUND(data_tbl.avg_overdrawn * 1000) / 10 AS ovr_pct,
  ROUND(data_tbl.avg_eff * 1000) / 10 AS joint_eff,
  ROUND(data_tbl.avg_opt * 1000) / 10 AS opt_rate
FROM data_tbl
ORDER BY data_tbl.pair, data_tbl.mc_bucket, data_tbl.share_projects DESC;

-- INTERVENTION: theory of mind
SELECT
  model,
  think_about_opponent,
  avg(fair_efficiency) AS fair_eff
FROM agents
WHERE mc_bucket = 0.5
  AND pair = 'qwen/qwen3.5-flash-02-23 vs claude-sonnet-4-5'
GROUP BY model, think_about_opponent;


WITH data_tbl AS (
SELECT
  pair,
  mc_bucket,
  think_about_opponent,
  avg(CAST(overdrawn AS int)) as avg_overdrawn,
  avg(joint_efficiency) as avg_eff,
  avg(CAST((joint_efficiency = 1) AS int)) as avg_opt,
FROM rounds
WHERE is_baseline = false
  AND model_a != 'gpt-5.4-mini-2026-03-17'
  AND pair = 'qwen/qwen3.5-flash-02-23 vs claude-sonnet-4-5'
  AND mc_bucket = 0.5
GROUP BY pair, mc_bucket, think_about_opponent
)
SELECT
  data_tbl.pair,
  data_tbl.mc_bucket,
  data_tbl.think_about_opponent,
  ROUND(data_tbl.avg_overdrawn * 1000) / 10 AS ovr_pct,
  ROUND(data_tbl.avg_eff * 1000) / 10 AS joint_eff,
  ROUND(data_tbl.avg_opt * 1000) / 10 AS opt_rate
FROM data_tbl
ORDER BY data_tbl.pair, data_tbl.mc_bucket, data_tbl.think_about_opponent DESC;
