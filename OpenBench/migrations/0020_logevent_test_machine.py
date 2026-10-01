from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('OpenBench', '0019_machine_host_key'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='logevent',
            index=models.Index(fields=['test_id', 'machine_id'], name='logevent_test_machine'),
        ),
    ]
