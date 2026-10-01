import json
import tempfile
from collections.abc import Iterable
from typing import Any

from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from OpenBench.models import Book, Engine, EngineConfig, LogEvent, Machine, Profile, Test
from OpenBench.triage.demo import BUILD_LOG, build_failure_summary, record_error
from OpenBench.upstream import openbench_config

PASSWORD = 'correct-horse-battery-staple'


def present[T](value: T | None) -> T:
    if value is None:
        raise AssertionError('expected a value, got None')
    return value


def use_temporary_media(case: SimpleTestCase) -> str:
    media = tempfile.TemporaryDirectory()
    case.addCleanup(media.cleanup)
    case.enterContext(override_settings(MEDIA_ROOT=media.name))
    return media.name


def create_user(username: str, enabled: bool = True, approver: bool = False) -> User:
    user = User.objects.create_user(username, '', PASSWORD)
    Profile.objects.create(user=user, enabled=enabled, approver=approver)
    return user


def create_engine_config(name: str = 'Avalanche', cpuflags: str = 'AVX2') -> EngineConfig:
    return EngineConfig.objects.create(
        name=name,
        nps=1000000,
        source='https://github.com/SnowballSH/Avalanche',
        build_path='',
        build_compilers='zig>=0.16.0',
        build_cpuflags=cpuflags,
        build_systems='Linux Darwin',
        presets={'test_presets': {'default': {}}, 'tune_presets': {'default': {}}, 'datagen_presets': {'default': {}}},
    )


def ensure_book() -> Book:
    return Book.objects.get_or_create(
        name='UHO_Lichess_4852_v1.epd', defaults={'source': 'https://example.invalid/book.zip', 'sha': '0' * 64}
    )[0]


def create_test(author: User, engine: str = 'Avalanche', threads: int = 1, priority: int = 0, **fields: Any) -> Test:
    dev = Engine.objects.create(name='dev', source='https://github.com/SnowballSH/Avalanche', sha='a' * 40, bench=1)
    base = Engine.objects.create(name='base', source='https://github.com/SnowballSH/Avalanche', sha='b' * 40, bench=1)
    options = f'Threads={threads} Hash=16'
    return Test.objects.create(
        **{
            'author': author.username,
            'book_name': 'UHO_Lichess_4852_v1.epd',
            'dev': dev,
            'dev_repo': dev.source,
            'dev_engine': engine,
            'dev_options': options,
            'dev_time_control': '8.0+0.08',
            'base': base,
            'base_repo': base.source,
            'base_engine': engine,
            'base_options': options,
            'base_time_control': '8.0+0.08',
            'approved': True,
            'priority': priority,
            'throughput': 1000,
            'elolower': 0.0,
            'eloupper': 3.0,
            'alpha': 0.05,
            'beta': 0.05,
            'lowerllr': -2.94,
            'upperllr': 2.94,
            **fields,
        }
    )


def system_info(
    concurrency: int = 4,
    physical_cores: int = 4,
    cpu_flags: Iterable[str] = ('AVX2',),
    engines: Iterable[str] = ('Avalanche',),
    **overrides: Any,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        'compilers': {name: ['zig', '0.16.0'] for name in engines},
        'tokens': {},
        'cpu_flags': list(cpu_flags),
        'os_name': 'Linux',
        'logical_cores': concurrency,
        'physical_cores': physical_cores,
        'ram_total_mb': 8192,
        'concurrency': concurrency,
        'sockets': 1,
        'syzygy_max': 2,
        'noisy': False,
        'client_ver': openbench_config()['client_version'],
    }
    info.update(overrides)
    return info


def credentials(user: User) -> dict[str, str]:
    return {'username': user.username, 'password': PASSWORD}


def register_payload(user: User, **info: Any) -> dict[str, str]:
    return {**credentials(user), 'system_info': json.dumps(system_info(**info))}


def logged_build_failure(case: SimpleTestCase, test: Test, machine: Machine) -> LogEvent:
    # Long enough to fold, with markup a careless template would let through
    use_temporary_media(case)
    log = BUILD_LOG + '<b onclick=x style=y>\n' * 400
    return record_error(test, machine.id, machine.user.username, build_failure_summary(test), timezone.now(), log)
