from threading import Barrier

import pytest

from immich_google_dedupe.util import ordered_parallel_map


def test_ordered_parallel_map_runs_concurrently_and_keeps_order():
    started = Barrier(2)

    def task(value: int) -> int:
        started.wait(timeout=5)
        return value

    assert list(ordered_parallel_map(task, [0, 1], workers=2)) == [0, 1]


def test_ordered_parallel_map_rejects_zero_workers():
    with pytest.raises(ValueError, match="workers must be at least 1"):
        list(ordered_parallel_map(lambda value: value, [1], workers=0))


def test_ordered_parallel_map_bounds_queued_work():
    submitted: list[int] = []

    def source():
        for value in range(100):
            submitted.append(value)
            yield value

    results = ordered_parallel_map(lambda value: value, source(), workers=2)
    assert next(results) == 0
    assert len(submitted) == 4
    assert list(results) == list(range(1, 100))
