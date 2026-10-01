from collections.abc import Iterable, Sequence
from typing import Any, cast

import OpenBench.utils
from OpenBench.models import Machine, Test
from OpenBench.workloads import get_workload

type Shares = dict[int, dict[str, Any]]


def active_workloads() -> list[Test]:
    return list(OpenBench.utils.get_active_tests().select_related('spsa_run').order_by('id'))


def unmet_syzygy(machine: Machine) -> list[str]:
    return cast(list[str], get_workload.unmet_syzygy_requirements(machine))


def uses_time_based_control(workload: Test) -> bool:
    return bool(OpenBench.utils.workload_uses_time_based_tc(workload))


def fits_hardware(workload: Test, machine: Machine) -> bool:
    return bool(get_workload.valid_hardware_assignment(workload, machine))


def thread_option(options: str) -> int:
    return int(OpenBench.utils.extract_option(options, 'Threads'))


def refine(options: Sequence[Test], machine: Machine) -> tuple[list[Test], bool]:
    candidates, has_focus = get_workload.refine_candidates(list(options), machine)
    return list(candidates), bool(has_focus)


def resource_ratios(candidates: Sequence[Test], machine: Machine, has_focus: bool, others: Iterable[Machine]) -> Shares:
    shares, engine_frequency = get_workload.distribute_resources(candidates, has_focus, others)
    get_workload.apply_resource_ratios(shares, engine_frequency, machine)
    return cast(Shares, shares)
