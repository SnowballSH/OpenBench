from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from django.core.exceptions import ObjectDoesNotExist

from OpenBench.models import Engine, SPSARun, Test
from OpenBench.spsa_utils import spsa_original_input

type WorkloadType = Literal['TEST', 'TUNE', 'DATAGEN']
type Side = Literal['dev', 'base']
type FormFields = dict[str, str]

ENGINE_FIELDS = (
    'engine',
    'repo',
    'branch',
    'bench',
    'network',
    'options',
    'time_control',
)

GENERAL_FIELDS = (
    'book_name',
    'upload_pgns',
    'info',
    'priority',
    'throughput',
    'syzygy_wdl',
    'syzygy_adj',
    'win_adj',
    'draw_adj',
    'scale_method',
    'scale_nps',
)

TEST_MODE_FIELDS = ('test_mode', 'test_bounds', 'test_confidence', 'test_max_games')

SPSA_FIELDS = (
    'spsa_inputs',
    'spsa_reporting_type',
    'spsa_distribution_type',
    'spsa_alpha',
    'spsa_gamma',
    'spsa_A_ratio',
    'spsa_iterations',
    'spsa_pairs_per',
)

DATAGEN_FIELDS = (
    'datagen_max_games',
    'datagen_custom_genfens',
    'datagen_play_reverses',
)


def _side_fields(side: Side) -> tuple[str, ...]:
    return tuple(f'{side}_{field}' for field in ENGINE_FIELDS)


FORM_FIELDS: dict[WorkloadType, tuple[str, ...]] = {
    'TEST': _side_fields('dev') + _side_fields('base') + GENERAL_FIELDS + ('workload_size',) + TEST_MODE_FIELDS,
    'TUNE': _side_fields('dev') + GENERAL_FIELDS + SPSA_FIELDS,
    'DATAGEN': _side_fields('dev') + _side_fields('base') + GENERAL_FIELDS + ('workload_size',) + DATAGEN_FIELDS,
}

NOT_APPLICABLE = 'N/A'

MAX_WORKLOAD_ID = 2**31 - 1


class CloneError(Exception):
    pass


@dataclass(frozen=True)
class CloneSource:
    id: int
    name: str
    url: str
    fields: FormFields


def workload_type_of(workload: Test) -> WorkloadType:
    match workload.test_mode:
        case 'SPSA':
            return 'TUNE'
        case 'DATAGEN':
            return 'DATAGEN'
        case _:
            return 'TEST'


def decimal_text(value: float) -> str:
    return format(Decimal(repr(value)), 'f')


def is_pinned(engine: Engine) -> bool:
    return engine.name.lower() == engine.sha.lower()


def pinned_bench(engine: Engine) -> str:
    return str(engine.bench) if is_pinned(engine) else ''


def test_info(workload: Test) -> FormFields:
    return {'info': workload.info if is_pinned(workload.dev) else ''}


def engine_fields(
    side: Side,
    engine: Engine,
    config: str,
    repo: str,
    network: str,
    options: str,
    time_control: str,
) -> FormFields:
    values = (
        config,
        repo,
        engine.name,
        pinned_bench(engine),
        network,
        options,
        time_control,
    )
    return dict(zip(_side_fields(side), values, strict=True))


def dev_fields(workload: Test) -> FormFields:
    return engine_fields(
        'dev',
        workload.dev,
        workload.dev_engine,
        workload.dev_repo,
        workload.dev_network,
        workload.dev_options,
        workload.dev_time_control,
    )


def base_fields(workload: Test) -> FormFields:
    return engine_fields(
        'base',
        workload.base,
        workload.base_engine,
        workload.base_repo,
        workload.base_network,
        workload.base_options,
        workload.base_time_control,
    )


def general_fields(workload: Test) -> FormFields:
    return {
        'book_name': workload.book_name,
        'upload_pgns': workload.upload_pgns,
        'info': workload.info,
        'priority': str(workload.priority),
        'throughput': str(workload.throughput),
        'syzygy_wdl': workload.syzygy_wdl,
        'syzygy_adj': workload.syzygy_adj,
        'win_adj': workload.win_adj,
        'draw_adj': workload.draw_adj,
        **scale_fields(workload),
    }


def scale_fields(workload: Test) -> FormFields:
    method = {'scale_method': workload.scale_method}
    if workload.scale_nps <= 0:
        return method
    return method | {'scale_nps': str(workload.scale_nps)}


def test_mode_fields(workload: Test) -> FormFields:
    if workload.test_mode == 'GAMES':
        return {
            'test_mode': 'GAMES',
            'test_bounds': NOT_APPLICABLE,
            'test_confidence': NOT_APPLICABLE,
            'test_max_games': str(workload.max_games),
        }
    lower, upper = decimal_text(workload.elolower), decimal_text(workload.eloupper)
    beta, alpha = decimal_text(workload.beta), decimal_text(workload.alpha)
    return {
        'test_mode': 'SPRT',
        'test_bounds': f'[{lower}, {upper}]',
        'test_confidence': f'[{beta}, {alpha}]',
        'test_max_games': NOT_APPLICABLE,
    }


def spsa_fields(workload: Test, spsa_run: SPSARun) -> FormFields:
    return {
        'spsa_inputs': spsa_original_input(workload),
        'spsa_reporting_type': spsa_run.reporting_type,
        'spsa_distribution_type': spsa_run.distribution_type,
        'spsa_alpha': decimal_text(spsa_run.alpha),
        'spsa_gamma': decimal_text(spsa_run.gamma),
        'spsa_A_ratio': decimal_text(spsa_run.a_ratio),
        'spsa_iterations': str(spsa_run.iterations),
        'spsa_pairs_per': str(spsa_run.pairs_per),
    }


def datagen_fields(workload: Test) -> FormFields:
    return {
        'datagen_max_games': str(workload.max_games),
        'datagen_custom_genfens': workload.genfens_args,
        'datagen_play_reverses': 'YES' if workload.play_reverses else 'NO',
    }


def clone_fields(workload: Test) -> FormFields:
    match workload_type_of(workload):
        case 'TEST':
            sections = (
                dev_fields(workload),
                base_fields(workload),
                general_fields(workload),
                test_info(workload),
                {'workload_size': str(workload.workload_size)},
                test_mode_fields(workload),
            )
        case 'TUNE':
            sections = (
                dev_fields(workload),
                general_fields(workload),
                spsa_fields(workload, workload.spsa_run),
            )
        case 'DATAGEN':
            sections = (
                dev_fields(workload),
                base_fields(workload),
                general_fields(workload),
                {'workload_size': str(workload.workload_size)},
                datagen_fields(workload),
            )
    return {name: value for section in sections for name, value in section.items()}


def submitted_fields(post: Mapping[str, str], workload_type: WorkloadType) -> FormFields:
    return {name: post[name] for name in FORM_FIELDS[workload_type] if name in post}


def parse_workload_id(raw_id: str) -> int | None:
    if not (raw_id.isascii() and raw_id.isdigit()):
        return None
    workload_id = int(raw_id)
    return workload_id if 1 <= workload_id <= MAX_WORKLOAD_ID else None


def load_clone_source(raw_id: str, workload_type: WorkloadType) -> CloneSource:
    if (workload_id := parse_workload_id(raw_id)) is None:
        raise CloneError('Nothing was cloned: the clone parameter is not a workload id')

    workload = Test.objects.select_related('dev', 'base').filter(id=workload_id).first()
    if workload is None:
        raise CloneError(f'Nothing was cloned: workload #{workload_id} does not exist')

    if (source_type := workload_type_of(workload)) != workload_type:
        raise CloneError(
            f'Nothing was cloned: workload #{workload.id} is a {source_type.lower()}, not a {workload_type.lower()}'
        )

    try:
        fields = clone_fields(workload)
    except ObjectDoesNotExist as error:
        raise CloneError(f'Nothing was cloned: workload #{workload.id} is incomplete') from error

    url = f'/{workload.workload_type_str()}/{workload.id}/'
    return CloneSource(id=workload.id, name=workload.dev.name, url=url, fields=fields)
