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


@dataclass(frozen=True, slots=True)
class ExistingConfirmation:
    id: int
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


def existing_confirmation(workload: Test) -> ExistingConfirmation | None:

    # The same two commits on the same engine and networks, already run or running at the long time control
    same_pair = Test.objects.filter(
        test_mode='SPRT',
        deleted=False,
        dev_engine=workload.dev_engine,
        base_engine=workload.base_engine,
        dev__sha=workload.dev.sha,
        base__sha=workload.base.sha,
        dev_network=workload.dev_network,
        base_network=workload.base_network,
    ).exclude(id=workload.id)

    found = next((test for test in same_pair.order_by('-id') if workload_time_class(test) == CONFIRMING_CLASS), None)
    if found is None:
        return None
    return ExistingConfirmation(found.id, found.dev_time_control, f'/test/{found.id}/')


def new_confirmation(workload: Test, profile: Profile | None) -> Confirmation | None:

    if profile is None or not profile.enabled:
        return None

    config = EngineConfig.objects.filter(name=workload.dev_engine, enabled=True).first()
    if config is None or (name := preset_of_class(config, CONFIRMING_CLASS)) is None:
        return None

    preset = test_preset(config, name) or {}
    query = urlencode({'clone': workload.id, 'preset': name})
    return Confirmation(preset=name, time_control=preset['dev_time_control'], url=f'/test/new/?{query}')


def confirmation_for(workload: Test, profile: Profile | None) -> Confirmation | ExistingConfirmation | None:
    if not awaits_confirmation(workload):
        return None
    return existing_confirmation(workload) or new_confirmation(workload, profile)
