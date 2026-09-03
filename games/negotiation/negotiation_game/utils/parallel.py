"""Utility for running a list of tasks either sequentially or concurrently.

Usage
-----
Pass a function and a list of items. At max_workers=1 (default) the function
is called in a plain loop — no thread overhead, identical behaviour to
writing the loop yourself. At max_workers>1 a ThreadPoolExecutor fans the
calls out concurrently.

    results, errors = run_with_parallelism(
        fn=judge_one,
        items=contexts,
        max_workers=args.max_parallelism,
        on_result=lambda ctx, r: log.info("done %s", ctx.episode_uid),
        on_error=lambda ctx, e: log.error("failed %s: %s", ctx.episode_uid, e),
    )

Returns
-------
results : list of (item, return_value) pairs for every successful call,
          in completion order (parallel) or input order (sequential).
errors  : list of (item, exception) pairs for every failed call.
"""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


def run_with_parallelism(
    fn: Callable[[T], R],
    items: list[T],
    max_workers: int = 1,
    on_result: Callable[[T, R], None] | None = None,
    on_error: Callable[[T, Exception], None] | None = None,
) -> tuple[list[tuple[T, R]], list[tuple[T, Exception]]]:
    """Run fn(item) for each item, sequentially or concurrently.

    Parameters
    ----------
    fn          : callable that takes a single item and returns a result.
    items       : list of items to process.
    max_workers : 1 = sequential loop (default); >1 = ThreadPoolExecutor.
    on_result   : optional callback fired after each successful call.
    on_error    : optional callback fired after each failed call.

    Returns
    -------
    (results, errors) where:
        results = [(item, return_value), ...]  — successful calls
        errors  = [(item, exception), ...]     — failed calls
    """
    results: list[tuple[T, R]] = []
    errors: list[tuple[T, Exception]] = []

    if not items:
        return results, errors

    if max_workers <= 1:
        for item in items:
            try:
                value = fn(item)
            except Exception as e:
                errors.append((item, e))
                if on_error:
                    on_error(item, e)  # propagates if on_error raises
                continue
            results.append((item, value))
            if on_result:
                on_result(item, value)  # propagates if on_result raises
    else:
        # Never spin up more threads than there are tasks
        workers = min(max_workers, len(items))
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fn, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    value = future.result()
                except Exception as e:
                    with lock:
                        errors.append((item, e))
                    if on_error:
                        on_error(item, e)  # propagates if on_error raises
                    continue
                with lock:
                    results.append((item, value))
                if on_result:
                    on_result(item, value)  # propagates if on_result raises

    return results, errors
