import hashlib
import json
import re
from itertools import batched
from typing import Any, ClassVar

from django.db import migrations, models

BATCH = 500
MAC_PATTERN = re.compile(r'[0-9A-Fa-f]{1,12}')
MAC_MULTICAST_BIT = 1 << 40


# The key rule as it stood when this migration was written, frozen here so that a
# later change to OpenBench.fleet.hosts needs its own migration to re-key rows
def text_of(info: Any, key: str) -> str | None:
    value = info.get(key) if isinstance(info, dict) else None
    return str(value) if value not in (None, '', 'None') else None


def int_of(info: Any, key: str) -> int:
    try:
        return int((info.get(key) if isinstance(info, dict) else 0) or 0)
    except TypeError, ValueError, OverflowError:
        return 0


def stable_mac(info: Any) -> str | None:
    value = info.get('mac_address') if isinstance(info, dict) else None
    if not isinstance(value, str) or not MAC_PATTERN.fullmatch(value):
        return None
    node = int(value, 16)
    return None if node == 0 or node & MAC_MULTICAST_BIT else f'{node:012X}'


def host_key(owner: str, info: Any) -> str:
    name = text_of(info, 'machine_name')
    parts = (
        owner,
        name,
        None if name else stable_mac(info),
        text_of(info, 'cpu_name'),
        text_of(info, 'os_name'),
        int_of(info, 'logical_cores'),
        int_of(info, 'physical_cores'),
    )
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]


def fill_host_keys(apps: Any, schema_editor: Any) -> None:
    Machine = apps.get_model('OpenBench', 'Machine')
    rows = Machine.objects.values_list('id', 'user__username', 'info').iterator(chunk_size=BATCH)
    for batch in batched(rows, BATCH):
        Machine.objects.bulk_update(
            [Machine(id=pk, host_key=host_key(owner, info)) for pk, owner, info in batch], ['host_key']
        )


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ('OpenBench', '0018_listing_and_queue_indexes'),
    ]

    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.AddField(
            model_name='machine',
            name='host_key',
            field=models.CharField(db_default='', default='', editable=False, max_length=32),
        ),
        migrations.RunPython(fill_host_keys, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='machine',
            index=models.Index(fields=['host_key', 'updated'], name='machine_host_updated'),
        ),
        migrations.AddIndex(
            model_name='machine',
            index=models.Index(fields=['user', 'updated'], name='machine_user_updated'),
        ),
    ]
