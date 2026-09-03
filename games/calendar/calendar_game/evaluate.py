"""Summarize CalBench trace directories from the command line."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from calendar_game.dataset import CalendarEpisodeDataset


SUMMARY_COLUMNS = [
    "n_games",
    "headline_score_mean",
    "headline_score_sem",
    "success_score_mean",
    "cost_score_mean",
    "privacy_score_mean",
    "efficiency_score_mean",
    "communication_score_mean",
    "excess_cost_score_mean",
    "coordination_rate_mean",
    "meetings_scheduled_mean",
    "msgs_per_meeting_mean",
    "dm_chars_per_meeting_mean",
    "realized_cost_mean",
    "optimal_cost_mean",
    "scheduled_only_optimal_cost_mean",
    "excess_cost_mean",
    "cost_regret_mean",
    "greedy_normalized_regret_mean",
    "total_dms_sent_mean",
    "privacy_leak_count_mean",
    "cost_ratio_mean",
    "cost_gini_mean",
    "fairness_metric_mean",
]


def summarize_game_df(game_df: pd.DataFrame) -> pd.DataFrame:
    if game_df.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    group_cols = [
        col
        for col in (
            "experiment_name",
            "dataset",
            "model_label",
            "difficulty",
            "num_agents",
            "num_participants",
            "density",
            "nosy_agent_count",
        )
        if col in game_df.columns
    ]
    if not group_cols:
        group_cols = ["experiment_name", "model_label"]
    grouped = game_df.groupby(group_cols, dropna=False)
    rows = []
    for key, group in grouped:
        if not isinstance(key, tuple):
            key = (key,)
        row = dict(zip(group_cols, key, strict=False))
        total_dms_col = "total_dms" if "total_dms" in group.columns else "total_dms_sent"
        def mean_col(name: str):
            return group[name].mean() if name in group.columns else None

        row.update({
            "n_games": len(group),
            "headline_score_mean": mean_col("headline_score"),
            "headline_score_sem": group["headline_score"].sem() if "headline_score" in group.columns else None,
            "success_score_mean": mean_col("success_score"),
            "cost_score_mean": mean_col("cost_score"),
            "privacy_score_mean": mean_col("privacy_score"),
            "efficiency_score_mean": mean_col("efficiency_score"),
            "communication_score_mean": mean_col("communication_score"),
            "excess_cost_score_mean": mean_col("excess_cost_score"),
            "coordination_rate_mean": group["coordination_rate"].mean(),
            "meetings_scheduled_mean": group["meetings_scheduled"].mean(),
            "msgs_per_meeting_mean": group["msgs_per_meeting"].mean(),
            "dm_chars_per_meeting_mean": group["dm_chars_per_meeting"].mean(),
            "realized_cost_mean": group["realized_cost"].mean(),
            "optimal_cost_mean": group["optimal_cost"].mean(),
            "scheduled_only_optimal_cost_mean": mean_col("scheduled_only_optimal_cost"),
            "excess_cost_mean": group["excess_cost"].mean(),
            "cost_regret_mean": mean_col("cost_regret"),
            "greedy_normalized_regret_mean": mean_col("greedy_normalized_regret"),
            "total_dms_sent_mean": group[total_dms_col].mean() if total_dms_col in group.columns else None,
            "privacy_leak_count_mean": mean_col("privacy_leak_count"),
            "cost_ratio_mean": group["cost_ratio"].mean(),
            "cost_gini_mean": group["cost_gini"].mean(),
            "fairness_metric_mean": group["fairness_metric"].mean(),
        })
        rows.append(row)
    return pd.DataFrame(rows).sort_values(group_cols)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize CalBench JSON episodes.")
    parser.add_argument("trace_dir", help="Trace directory, usually results/<experiment_name>")
    parser.add_argument("--environment-csv", help="Optional path for one-row-per-environment CSV output.")
    parser.add_argument("--round-csv", help="Optional path for one-row-per-round CSV output.")
    parser.add_argument("--agent-csv", help="Optional path for one-row-per-agent CSV output.")
    parser.add_argument("--message-csv", help="Optional path for one-row-per-DM CSV output.")
    parser.add_argument("--summary-csv", help="Optional path for grouped summary CSV output.")
    args = parser.parse_args(argv)

    ds = CalendarEpisodeDataset.from_dir(args.trace_dir)
    game_df = ds.to_game_df()
    summary_df = summarize_game_df(game_df)

    if args.game_csv:
        Path(args.game_csv).parent.mkdir(parents=True, exist_ok=True)
        game_df.to_csv(args.game_csv, index=False)
    if args.round_csv:
        Path(args.round_csv).parent.mkdir(parents=True, exist_ok=True)
        ds.to_round_df().to_csv(args.round_csv, index=False)
    if args.agent_csv:
        Path(args.agent_csv).parent.mkdir(parents=True, exist_ok=True)
        ds.to_agent_df().to_csv(args.agent_csv, index=False)
    if args.message_csv:
        Path(args.message_csv).parent.mkdir(parents=True, exist_ok=True)
        ds.to_message_df().to_csv(args.message_csv, index=False)
    if args.summary_csv:
        Path(args.summary_csv).parent.mkdir(parents=True, exist_ok=True)
        summary_df.to_csv(args.summary_csv, index=False)

    if summary_df.empty:
        print(f"No episodes found under {args.trace_dir}")
    else:
        print(summary_df.to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
