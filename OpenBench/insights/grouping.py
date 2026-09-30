from collections.abc import Callable, Iterable, Sequence

def sum_by_key[R, K](rows: Iterable[R], key_of: Callable[[R], K | None], values_of: Callable[[R], Sequence[int]], missing: K) -> dict[K, list[int]]:

    totals: dict[K, list[int]] = {}

    for row in rows:
        values = values_of(row)
        key    = key_of(row) or missing
        total  = totals.setdefault(key, [0] * len(values))
        for index, value in enumerate(values):
            total[index] += value

    return totals
