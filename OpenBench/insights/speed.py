from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpeedCounters:
    dev_nodes: int = 0
    dev_time: int = 0
    dev_time_scaled: int = 0
    base_nodes: int = 0
    base_time: int = 0
    base_time_scaled: int = 0

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.dev_nodes,
            self.dev_time,
            self.dev_time_scaled,
            self.base_nodes,
            self.base_time,
            self.base_time_scaled,
        )


def nodes_per_second(nodes: int, time_ms: int) -> int:
    return round(1000 * nodes / time_ms) if nodes and time_ms else 0
