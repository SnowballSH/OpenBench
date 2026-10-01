from dataclasses import dataclass
from urllib.parse import urlencode

from OpenBench.models import EngineConfig, Profile, Test
from OpenBench.progress.conditions import time_class
from OpenBench.progress.domain import TimeClass
from OpenBench.workloads.presets import preset_of_class, test_preset

CONFIRMED_CLASS = TimeClass.STC
CONFIRMING_CLASS = TimeClass.LTC


@dataclass(frozen=True, slots=True)
class Confirmation:
    preset: str
    time_control: str
    url: str


def workload_time_class(workload: Test) -> TimeClass:
    return time_class(
        workload.dev_time_control, workload.base_time_control, workload.dev_options, workload.base_options
    )


def awaits_confirmation(workload: Test) -> bool:
    return (
        workload.test_mode == 'SPRT'
        and workload.finished
        and workload.passed
        and not workload.deleted
        and workload.dev_engine == workload.base_engine
        and workload_time_class(workload) == CONFIRMED_CLASS
    )


def confirmation_for(workload: Test, profile: Profile | None) -> Confirmation | None:

    if profile is None or not profile.enabled or not awaits_confirmation(workload):
        return None

    config = EngineConfig.objects.filter(name=workload.dev_engine, enabled=True).first()
    if config is None or (name := preset_of_class(config, CONFIRMING_CLASS)) is None:
        return None

    preset = test_preset(config, name) or {}
    query = urlencode({'clone': workload.id, 'preset': name})
    return Confirmation(preset=name, time_control=preset['dev_time_control'], url=f'/test/new/?{query}')
