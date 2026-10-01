from dataclasses import dataclass
from enum import StrEnum

from OpenBench.machine_info import SystemInfo
from OpenBench.models import EngineConfig


class Requirement(StrEnum):
    CPU_FLAG = 'cpu_flag'
    GIT_TOKEN = 'git_token'  # noqa: S105 - a requirement kind, not a credential
    COMPILER = 'compiler'
    OPERATING_SYSTEM = 'operating_system'


@dataclass(frozen=True, slots=True)
class MissingRequirement:
    kind: Requirement
    detail: str


def missing_requirements(config: EngineConfig, info: SystemInfo) -> list[MissingRequirement]:

    build = config.build()
    missing = [
        MissingRequirement(Requirement.CPU_FLAG, flag) for flag in build['cpuflags'] if flag not in info['cpu_flags']
    ]

    if config.private and config.name not in info['tokens']:
        missing.append(MissingRequirement(Requirement.GIT_TOKEN, config.name))

    if not config.private and config.name not in info['compilers']:
        missing.append(MissingRequirement(Requirement.COMPILER, ' or '.join(build['compilers'])))

    if info['os_name'] not in build['systems']:
        missing.append(MissingRequirement(Requirement.OPERATING_SYSTEM, info['os_name']))

    return missing
