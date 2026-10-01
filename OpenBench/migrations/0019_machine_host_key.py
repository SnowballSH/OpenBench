from itertools import batched
from typing import Any, ClassVar

from django.db import migrations, models

from OpenBench.fleet.hosts import host_key

BATCH = 500


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
            field=models.CharField(default='', editable=False, max_length=32),
        ),
        migrations.RunPython(fill_host_keys, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='machine',
            index=models.Index(fields=['host_key', 'updated'], name='machine_host_updated'),
        ),
    ]
