from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


def seed_history(apps, schema_editor):
    Test = apps.get_model('OpenBench', 'Test')
    History = apps.get_model('OpenBench', 'LLRHistory')
    database = schema_editor.connection.alias
    batch = []
    tests = Test.objects.using(database).filter(test_mode='SPRT').only('id', 'games', 'currentllr')
    for test in tests.iterator(chunk_size=1000):
        test.llr_history_state = {'count': 1, 'last_games': test.games}
        batch.append(test)
        if len(batch) == 1000:
            History.objects.using(database).bulk_create([
                History(test_id=item.id, games=item.games, llr=item.currentllr) for item in batch
            ])
            Test.objects.using(database).bulk_update(batch, ['llr_history_state'])
            batch = []
    if batch:
        History.objects.using(database).bulk_create([
            History(test_id=item.id, games=item.games, llr=item.currentllr) for item in batch
        ])
        Test.objects.using(database).bulk_update(batch, ['llr_history_state'])


class Migration(migrations.Migration):

    replaces = [
        ('OpenBench', '0015_merge_configuration_and_nps_tracking'),
        ('OpenBench', '0016_configuration_foundation'),
        ('OpenBench', '0017_llr_history'),
        ('OpenBench', '0018_bound_llr_history'),
        ('OpenBench', '0019_gap_sample_llr_history'),
        ('OpenBench', '0018_simplify_configuration'),
        ('OpenBench', '0019_restore_variant_and_runner_management'),
    ]

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('OpenBench', '0014_merge_legacy_scale_and_nps_tracking'),
    ]

    operations = [
        migrations.CreateModel(
            name='EngineConfig',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='Runner',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='RunnerRelease',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('runner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='releases', to='OpenBench.runner')),
            ],
        ),
        migrations.AddField(
            model_name='test',
            name='llr_history_state',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name='WorkloadPreset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('workload_type', models.CharField(choices=[('TEST', 'Test'), ('TUNE', 'Tune'), ('DATAGEN', 'Datagen')], max_length=8)),
                ('position', models.PositiveIntegerField(default=0)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('engine', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='presets', to='OpenBench.engineconfig')),
                ('owner', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['position', 'name', 'id'],
            },
        ),
        migrations.CreateModel(
            name='Variant',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('runner_release', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='OpenBench.runnerrelease')),
            ],
        ),
        migrations.CreateModel(
            name='OpeningBook',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('variant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.variant')),
            ],
        ),
        migrations.CreateModel(
            name='LLRHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('games', models.IntegerField()),
                ('llr', models.FloatField()),
                ('test', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='llr_history', to='OpenBench.test')),
            ],
            options={
                'ordering': ['games'],
            },
        ),
        migrations.AddField(
            model_name='engineconfig',
            name='variants',
            field=models.ManyToManyField(blank=True, related_name='engines', to='OpenBench.variant'),
        ),
        migrations.AddConstraint(
            model_name='workloadpreset',
            constraint=models.UniqueConstraint(condition=models.Q(('owner__isnull', True)), fields=('engine', 'workload_type', 'name'), name='unique_shared_preset'),
        ),
        migrations.AddConstraint(
            model_name='workloadpreset',
            constraint=models.UniqueConstraint(condition=models.Q(('owner__isnull', False)), fields=('engine', 'owner', 'workload_type', 'name'), name='unique_personal_preset'),
        ),
        migrations.AddConstraint(
            model_name='llrhistory',
            constraint=models.UniqueConstraint(fields=('test', 'games'), name='unique_test_llr_games'),
        ),
        migrations.RunPython(seed_history, migrations.RunPython.noop),
    ]
