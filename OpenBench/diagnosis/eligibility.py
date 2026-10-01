from collections.abc import Mapping

from OpenBench.diagnosis import scheduler
from OpenBench.diagnosis.domain import Obstacle, ObstacleKind
from OpenBench.diagnosis.engine_support import MissingRequirement, Requirement, missing_requirements
from OpenBench.machine_info import SystemInfo
from OpenBench.models import EngineConfig, Machine, Test

REQUIREMENT_TEXT: dict[Requirement, str] = {
    Requirement.CPU_FLAG: 'lacks the CPU flag {}',
    Requirement.GIT_TOKEN: 'has no Git token for the private engine {}',
    Requirement.COMPILER: 'has no compiler for it ({})',
    Requirement.OPERATING_SYSTEM: 'runs {}, which the engine does not build on',
}
UNREADABLE = 'its registration or this workload cannot be evaluated'
BLACKLISTED = 'reported a build failure for this workload; the Client refuses it for the rest of that session'
NOISY = 'runs with --noisy, which only takes fixed-node or fixed-depth workloads'


def requirement_text(missing: MissingRequirement) -> str:
    return REQUIREMENT_TEXT[missing.kind].format(missing.detail)


def unsupported_detail(engine: str, config: EngineConfig | None, info: SystemInfo) -> str:

    if config is None:
        return f'cannot build {engine}: the engine is no longer configured'

    missing = missing_requirements(config, info)
    if not missing:
        return f'did not support {engine} when it registered'
    return f'cannot build {engine}: ' + '; '.join(requirement_text(item) for item in missing)


def threads_detail(workload: Test, info: SystemInfo) -> str:

    dev, base = scheduler.thread_option(workload.dev_options), scheduler.thread_option(workload.base_options)
    pair = ', two games at a time' if workload.test_mode == 'SPSA' else ''
    needed = max(dev, base) * (2 if pair else 1)
    halved = dev != base and info['physical_cores'] < info['concurrency']
    offered = (
        f'{info["concurrency"] // 2} (hyperthreads do not count for thread odds)' if halved else info['concurrency']
    )
    return f'needs {needed} threads (Threads={max(dev, base)}{pair}); the machine offers {offered}'


def syzygy_detail(workload: Test, unmet: list[str], info: SystemInfo) -> str:
    needed = ' and '.join(
        dict.fromkeys(value for value in (workload.syzygy_wdl, workload.syzygy_adj) if value in unmet)
    )
    return f'needs {needed} Syzygy tablebases; the machine has up to {info["syzygy_max"]}-MAN'


def evaluate(
    workload: Test, machine: Machine, configs: Mapping[str, EngineConfig], build_failures: frozenset[tuple[int, int]]
) -> list[Obstacle]:

    # The same exclusions, in the same order, as get_workload.filter_valid_workloads
    info: SystemInfo = machine.info
    found: list[Obstacle] = []

    for engine in dict.fromkeys((workload.dev_engine, workload.base_engine)):
        if engine not in info['supported']:
            detail = unsupported_detail(engine, configs.get(engine), info)
            found.append(Obstacle(ObstacleKind.ENGINE_UNSUPPORTED, detail))

    if (only := info.get('only', [])) and workload.dev_engine not in only:
        found.append(Obstacle(ObstacleKind.ONLY_EXCLUDES, f'runs with --only {" ".join(only)}'))

    if (workload.id, machine.id) in build_failures:
        found.append(Obstacle(ObstacleKind.BLACKLISTED, BLACKLISTED))

    unmet = scheduler.unmet_syzygy(machine)
    if workload.syzygy_adj in unmet or workload.syzygy_wdl in unmet:
        found.append(Obstacle(ObstacleKind.SYZYGY, syzygy_detail(workload, unmet, info)))

    if info.get('noisy') and scheduler.uses_time_based_control(workload):
        found.append(Obstacle(ObstacleKind.NOISY, NOISY))

    if not scheduler.fits_hardware(workload, machine):
        found.append(Obstacle(ObstacleKind.THREADS, threads_detail(workload, info)))

    return found


def obstacles(
    workload: Test, machine: Machine, configs: Mapping[str, EngineConfig], build_failures: frozenset[tuple[int, int]]
) -> list[Obstacle]:
    try:
        return evaluate(workload, machine, configs, build_failures)
    except KeyError, TypeError, ValueError, AttributeError:
        return [Obstacle(ObstacleKind.UNREADABLE, UNREADABLE)]
