import json
from pathlib import Path

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


def seed_history(apps, schema_editor):
    Test = apps.get_model('OpenBench', 'Test')
    History = apps.get_model('OpenBench', 'LLRHistory')
    database = schema_editor.connection.alias
    batch = []
    tests = Test.objects.using(database).filter(test_mode='SPRT', llr_history__isnull=True).only('id', 'games', 'currentllr')
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


def preserve_existing_execution(apps, schema_editor):
    database = schema_editor.connection.alias
    site = json.loads((Path(settings.BASE_DIR) / 'Config' / 'config.json').read_text(encoding='utf-8-sig'))
    Test = apps.get_model('OpenBench', 'Test')
    batch = []
    for test in Test.objects.using(database).filter(execution={}).only('id', 'book_name').iterator(chunk_size=1000):
        variant = 'fischerandom' if any(marker in test.book_name.upper() for marker in ('FRC', '960', 'FISCHER')) else 'standard'
        test.execution = {'variant': variant, 'fastchess_variant': variant, 'syzygy': True,
            'runner': {key: site['fastchess_' + key] for key in ('repo_url', 'repo_ref', 'min_version')}}
        batch.append(test)
        if len(batch) == 1000:
            Test.objects.using(database).bulk_update(batch, ['execution'])
            batch = []
    if batch:
        Test.objects.using(database).bulk_update(batch, ['execution'])


class ApplyMissingSchema(migrations.SeparateDatabaseAndState):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        state = from_state.clone()
        for operation in self.state_operations:
            next_state = state.clone()
            operation.state_forwards(app_label, next_state)
            name = operation.name if isinstance(operation, migrations.CreateModel) else operation.model_name
            model = next_state.apps.get_model(app_label, name)
            table = model._meta.db_table
            with schema_editor.connection.cursor() as cursor:
                introspection = schema_editor.connection.introspection
                tables = introspection.table_names(cursor)
                if isinstance(operation, migrations.CreateModel):
                    missing = table not in tables
                elif isinstance(operation, migrations.AddField):
                    field = model._meta.get_field(operation.name)
                    if field.many_to_many:
                        missing = field.remote_field.through._meta.db_table not in tables
                    else:
                        missing = field.column not in {column.name for column in introspection.get_table_description(cursor, table)}
                elif isinstance(operation, migrations.AddConstraint):
                    missing = operation.constraint.name not in introspection.get_constraints(cursor, table)
                else:
                    raise TypeError('Unsupported schema operation: %s' % type(operation).__name__)
            if missing:
                operation.database_forwards(app_label, schema_editor, state, next_state)
            state = next_state

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        from django.db.migrations.exceptions import IrreversibleError
        raise IrreversibleError('The combined migration preserves schema from earlier PR migrations')


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('OpenBench', '0014_merge_legacy_scale_and_nps_tracking'),
    ]

    schema_operations = [
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
        migrations.AddField(model_name='test', name='execution', field=models.JSONField(blank=True, default=dict)),
    ]

    operations = [
        ApplyMissingSchema(state_operations=schema_operations),
        migrations.RunPython(seed_history, migrations.RunPython.noop),
        migrations.RunPython(preserve_existing_execution, migrations.RunPython.noop),
    ]
