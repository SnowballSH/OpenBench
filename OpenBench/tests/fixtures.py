import json

from django.contrib.auth.models import User

from OpenBench.config import OPENBENCH_CONFIG
from OpenBench.models import Book, Engine, EngineConfig, Profile, Test

PASSWORD = 'correct-horse-battery-staple'


def create_user(username, enabled=True, approver=False):
    user = User.objects.create_user(username, '', PASSWORD)
    Profile.objects.create(user=user, enabled=enabled, approver=approver)
    return user


def create_engine_config(name='Avalanche', cpuflags='AVX2'):
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


def ensure_book():
    return Book.objects.get_or_create(
        name='UHO_Lichess_4852_v1.epd', defaults={'source': 'https://example.invalid/book.zip', 'sha': '0' * 64}
    )[0]


def create_test(author, engine='Avalanche', threads=1, priority=0, **fields):
    dev = Engine.objects.create(name='dev', source='https://github.com/SnowballSH/Avalanche', sha='a' * 40, bench=1)
    base = Engine.objects.create(name='base', source='https://github.com/SnowballSH/Avalanche', sha='b' * 40, bench=1)
    options = 'Threads=%d Hash=16' % (threads)
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


def system_info(concurrency=4, physical_cores=4, cpu_flags=('AVX2',), engines=('Avalanche',), **overrides):
    info = {
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
        'client_ver': OPENBENCH_CONFIG['client_version'],
    }
    info.update(overrides)
    return info


def credentials(user):
    return {'username': user.username, 'password': PASSWORD}


def register_payload(user, **info):
    return {**credentials(user), 'system_info': json.dumps(system_info(**info))}
