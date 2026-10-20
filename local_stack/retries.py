"""Select explicit new attempts from ended launches, preserving prior evidence."""

ACTIVE = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "CANCELLING"}


def retry_options(launches, attempts):
    if any(row["status"] in ACTIVE for row in launches):
        return []
    by_id = {row["id"]: row for row in launches}
    latest = {}
    for attempt in attempts:
        launch = by_id.get(attempt["launch_id"])
        if launch is None:
            continue
        key = (launch["mode"], attempt["episode_id"])
        previous = latest.get(key)
        if previous is None or int(attempt["attempt"]) > int(previous["attempt"]):
            latest[key] = attempt
    options = []
    for mode in ("live", "smoke", "dry_run"):
        incomplete = sorted(episode_id for (entry_mode, episode_id), attempt in latest.items()
                            if entry_mode == mode and
                            (attempt["status"] not in {"COMPLETED", "DRY_RUN"} or
                             by_id[attempt["launch_id"]].get("evidence_complete") is False))
        if incomplete:
            newest = max((row for row in launches if row["mode"] == mode),
                         key=lambda row: row.get("created_at") or "")
            options.append({"mode": mode, "episode_ids": incomplete,
                            "max_parallelism": newest["max_parallelism"]})
    return options


def select_retry_configs(configs, launches, attempts, *, mode, episode_ids=None):
    if any(row["status"] in ACTIVE for row in launches):
        raise ValueError("Wait for active experiment launches to finish before retrying")
    if episode_ids is not None and (not isinstance(episode_ids, list) or not episode_ids or
                                    any(not isinstance(value, str) or not value for value in episode_ids)):
        raise ValueError("episode_ids must be a non-empty list of episode IDs")
    eligible = next((set(option["episode_ids"]) for option in retry_options(launches, attempts)
                     if option["mode"] == mode), set())
    selected = set(episode_ids) if episode_ids is not None else eligible
    if not selected or not selected <= eligible:
        raise ValueError("No retryable episodes selected; completed or unattempted episodes cannot be retried")
    result = [config for config in configs if config["episode_id"] in selected]
    if {config["episode_id"] for config in result} != selected:
        raise ValueError("Selected episodes are absent from the frozen execution plan")
    return result
