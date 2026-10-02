from dataclasses import dataclass
from urllib.parse import urlencode

from OpenBench.models import EngineConfig
from OpenBench.progress.domain import TimeClass
from OpenBench.releases import store
from OpenBench.releases.domain import ReleaseAnchor
from OpenBench.workloads.clone import FORM_FIELDS, MAX_PRESET_NAME_LENGTH, NOT_APPLICABLE, FormFields
from OpenBench.workloads.presets import (
    DEFAULT_PRESET,
    Preset,
    preset_time_class,
    run_settings,
    test_preset,
    test_presets,
)

MEASURED_CLASSES = (TimeClass.STC, TimeClass.LTC)
DEFAULT_GAMES: dict[TimeClass, int] = {TimeClass.STC: 10_000, TimeClass.LTC: 5_000}
FALLBACK_GAMES = DEFAULT_GAMES[TimeClass.STC]
SPRT_ONLY_FIELDS = frozenset({'test_bounds', 'test_confidence'})
GAMES_FIELD = 'test_max_games'
MAX_ENGINE_NAME_LENGTH = 64


class MeasurementError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class MeasurementOption:
    label: str
    preset: str
    games: int
    time_control: str
    url: str

    @property
    def games_text(self) -> str:
        return f'{self.games:,}'


@dataclass(frozen=True, slots=True)
class MeasurementPrefill:
    engine: str
    tag: str
    default_branch: str
    preset: str
    games: int
    fields: FormFields

    @property
    def games_text(self) -> str:
        return f'{self.games:,}'


def named_presets(config: EngineConfig) -> dict[str, Preset]:
    names = (name for name in test_presets(config) if name != DEFAULT_PRESET)
    return {name: preset for name in names if (preset := test_preset(config, name)) is not None}


def own_games(config: EngineConfig, name: str) -> int | None:
    # Only a count the preset states itself: the default preset's one is the SPRT form's fallback
    raw = str(test_presets(config).get(name, {}).get(GAMES_FIELD, ''))
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


def preset_for_class(config: EngineConfig, wanted: TimeClass) -> str | None:
    # A preset written for fixed-games runs at this class outranks the SPRT one, then the class's own name
    matching = [name for name, preset in named_presets(config).items() if preset_time_class(preset) == wanted]
    ranked = sorted(matching, key=lambda name: (own_games(config, name) is None, name.lower() != wanted.value))
    return ranked[0] if ranked else None


def games_for(config: EngineConfig, name: str, preset: Preset) -> int:
    return own_games(config, name) or DEFAULT_GAMES.get(preset_time_class(preset), FALLBACK_GAMES)


def measurement_url(engine: str, preset: str) -> str:
    return f'/test/new/?{urlencode({"release": engine, "preset": preset})}'


def measurement_options(config: EngineConfig) -> list[MeasurementOption]:
    presets = named_presets(config)
    return [
        MeasurementOption(
            label=time_class.label,
            preset=name,
            games=games_for(config, name, presets[name]),
            time_control=presets[name].get('dev_time_control', ''),
            url=measurement_url(config.name, name),
        )
        for time_class in MEASURED_CLASSES
        if (name := preset_for_class(config, time_class)) is not None
    ]


def options_for(engine: str, anchor: ReleaseAnchor | None) -> list[MeasurementOption]:
    if anchor is None or not anchor.known or not anchor.default_branch:
        return []
    config = EngineConfig.objects.filter(name=engine, enabled=True).first()
    return measurement_options(config) if config else []


def measurement_fields(config: EngineConfig, anchor: ReleaseAnchor, preset: Preset, games: int) -> FormFields:
    settings = {
        name: value
        for name, value in run_settings(preset).items()
        if name in FORM_FIELDS['TEST'] and name not in SPRT_ONLY_FIELDS
    }
    return settings | {
        'dev_engine': config.name,
        'base_engine': config.name,
        'dev_repo': config.source,
        'base_repo': config.source,
        'dev_branch': anchor.default_branch,
        'base_branch': anchor.tag,
        'info': f'{anchor.default_branch} against the release {anchor.tag}',
        'test_mode': 'GAMES',
        'test_bounds': NOT_APPLICABLE,
        'test_confidence': NOT_APPLICABLE,
        GAMES_FIELD: str(games),
    }


def load_measurement(engine: str, preset_name: str | None) -> MeasurementPrefill:
    config = EngineConfig.objects.filter(name=engine[:MAX_ENGINE_NAME_LENGTH], enabled=True).first()
    if config is None:
        raise MeasurementError('Nothing was filled in: no enabled engine has that name')

    anchor = store.load_anchor(config.name)
    if anchor is None or not anchor.known or not anchor.default_branch:
        raise MeasurementError(f'Nothing was filled in: no release of {config.name} is known yet')

    name = (preset_name or '')[:MAX_PRESET_NAME_LENGTH]
    preset = named_presets(config).get(name)
    if preset is None:
        raise MeasurementError(f'Nothing was filled in: {config.name} has no test preset with that name')

    games = games_for(config, name, preset)
    return MeasurementPrefill(
        engine=config.name,
        tag=anchor.tag,
        default_branch=anchor.default_branch,
        preset=name,
        games=games,
        fields=measurement_fields(config, anchor, preset, games),
    )
