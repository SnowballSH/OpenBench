from collections.abc import Mapping

from OpenBench.models import EngineConfig
from OpenBench.progress.conditions import time_class
from OpenBench.progress.domain import TimeClass

type Preset = dict[str, str]
type Side = str

DEFAULT_PRESET = 'default'
SIDES: tuple[Side, ...] = ('dev', 'base')
SHARED_PREFIX = 'both_'

# What a workload is, rather than how it is run, so a preset never replaces them on a clone
IDENTITY_FIELDS = frozenset(f'{side}_{field}' for side in SIDES for field in ('branch', 'bench', 'network'))


def test_presets(config: EngineConfig) -> Mapping[str, Mapping[str, object]]:
    presets: Mapping[str, Mapping[str, object]] = config.presets.get('test_presets', {})
    return presets


def for_each_side(key: str, value: str) -> Preset:
    if not key.startswith(SHARED_PREFIX):
        return {key: value}
    return {f'{side}_{key.removeprefix(SHARED_PREFIX)}': value for side in SIDES}


def expanded(preset: Mapping[str, object]) -> Preset:
    # A key for one side outranks the shared one, whatever order the engine config lists them in
    shared_first = sorted(preset.items(), key=lambda item: not item[0].startswith(SHARED_PREFIX))
    return {name: text for key, value in shared_first for name, text in for_each_side(key, str(value)).items()}


def test_preset(config: EngineConfig, name: str) -> Preset | None:
    presets = test_presets(config)
    if name == DEFAULT_PRESET or name not in presets:
        return None
    return expanded(presets.get(DEFAULT_PRESET, {})) | expanded(presets[name])


def preset_time_class(preset: Preset) -> TimeClass:
    return time_class(
        preset.get('dev_time_control', ''),
        preset.get('base_time_control', ''),
        preset.get('dev_options', ''),
        preset.get('base_options', ''),
    )


def preset_of_class(config: EngineConfig, wanted: TimeClass) -> str | None:
    named = (name for name in test_presets(config) if name != DEFAULT_PRESET)
    matching = [name for name in named if preset_time_class(test_preset(config, name) or {}) == wanted]
    return next((name for name in matching if name.lower() == wanted.value), matching[0] if matching else None)


def run_settings(preset: Preset) -> Preset:
    return {name: value for name, value in preset.items() if name not in IDENTITY_FIELDS}
