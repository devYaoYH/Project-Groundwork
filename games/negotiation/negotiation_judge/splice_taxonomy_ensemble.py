"""Splice calibrated taxonomy judge outputs into final analysis CSVs."""

from pathlib import Path

import pandas as pd


KEYS = ["episode_uid", "round_number"]

METADATA_COLS = [
    "episode_uid",
    "round_number",
    "experiment_label",
    "episode_id",
    "model_a",
    "model_b",
    "mode",
    "mc_ratio",
    "round_outcome",
    "joint_efficiency",
]

GENERAL_COLS = [
    "misaligned_mental_models",
    "agreement_abandonment",
    "domain_specialization",
    "fairness_appeal",
    "fairness_appeal_agent_a",
    "fairness_appeal_agent_b",
    "threatening_language",
    "threatening_language_agent_a",
    "threatening_language_agent_b",
]

SPECIALIST_COLS = [
    "coordination_withdrawal",
    "misalignment_recovery",
    "voluntary_project_disclosure",
    "voluntary_project_disclosure_agent_a",
    "voluntary_project_disclosure_agent_b",
]

COHORTS = {
    "main720": {
        "general": Path(
            "judge/output/taxonomy_runs/main720_gemini31pro_temp0/"
            "main720_gemini31pro_temp0.csv"
        ),
        "specialist": Path(
            "judge/output/taxonomy_runs/main720_claude_opus46_temp0_v6_specialist/"
            "main720_claude_opus46_temp0_v6_specialist.csv"
        ),
        "output": Path(
            "judge/output/taxonomy_runs/final/"
            "main720_taxonomy_ensemble_v3_pro_v6_opus.csv"
        ),
    },
    "transparency120": {
        "general": Path(
            "judge/output/taxonomy_runs/transparency120_gemini31pro_temp0/"
            "transparency120_gemini31pro_temp0.csv"
        ),
        "specialist": Path(
            "judge/output/taxonomy_runs/transparency120_claude_opus46_temp0_v6_specialist/"
            "transparency120_claude_opus46_temp0_v6_specialist.csv"
        ),
        "output": Path(
            "judge/output/taxonomy_runs/final/"
            "transparency120_taxonomy_ensemble_v3_pro_v6_opus.csv"
        ),
    },
}


def splice_cohort(name: str, paths: dict[str, Path]) -> pd.DataFrame:
    general = pd.read_csv(paths["general"])
    specialist = pd.read_csv(paths["specialist"])

    final = general[METADATA_COLS + GENERAL_COLS].merge(
        specialist[KEYS + SPECIALIST_COLS],
        on=KEYS,
        how="left",
        validate="one_to_one",
    )

    missing_mask = final[SPECIALIST_COLS].isna().any(axis=1)
    if missing_mask.any():
        missing = final.loc[missing_mask, KEYS].head(20)
        raise RuntimeError(
            f"{name}: missing specialist labels for some rows:\n{missing}"
        )

    paths["output"].parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(paths["output"], index=False)
    return final


def main() -> None:
    for name, paths in COHORTS.items():
        final = splice_cohort(name, paths)
        print(f"{name}: wrote {len(final)} rows -> {paths['output']}")


if __name__ == "__main__":
    main()
