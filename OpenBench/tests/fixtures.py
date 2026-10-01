import json
from collections.abc import Iterable
from typing import Any

from django.contrib.auth.models import User

from OpenBench.models import Book, Engine, EngineConfig, Profile, Test
from OpenBench.upstream import openbench_config

PASSWORD = 'correct-horse-battery-staple'


def present[T](value: T | None) -> T:
    if value is None:
        raise AssertionError('expected a value, got None')
    return value


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


STC_PRESET = {'both_options': 'Threads=1 Hash=16', 'both_time_control': '8.0+0.08'}
LTC_PRESET = {'both_options': 'Threads=1 Hash=64', 'both_time_control': '40.0+0.40', 'workload_size': 8, 'priority': 2}


def set_test_presets(config: EngineConfig, default: dict[str, Any], **presets: dict[str, Any]) -> EngineConfig:
    config.presets = {**config.presets, 'test_presets': {'default': default, **presets}}
    config.save()
    return config


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
