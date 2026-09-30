def nodes_per_second(nodes: int, time_ms: int) -> int:
    return round(1000 * nodes / time_ms) if nodes and time_ms else 0
