from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('OpenBench', '0013_merge_legacy_scale_migration'),
    ]

    operations = [
        migrations.CreateModel(
            name='EngineConfig',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('schema_version', models.PositiveIntegerField(default=1)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Runner',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('schema_version', models.PositiveIntegerField(default=1)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='RunnerRelease',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('schema_version', models.PositiveIntegerField(default=1)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('runner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='releases', to='OpenBench.runner')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SiteSettings',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('schema_version', models.PositiveIntegerField(default=1)),
                ('generation', models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name='WorkloadPreset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('workload_type', models.CharField(choices=[('TEST', 'Test'), ('TUNE', 'Tune'), ('DATAGEN', 'Datagen')], max_length=8)),
                ('position', models.PositiveIntegerField(default=0)),
                ('schema_version', models.PositiveIntegerField(default=1)),
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
                ('schema_version', models.PositiveIntegerField(default=1)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('runner_release', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='OpenBench.runnerrelease')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='OpeningBook',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128, unique=True)),
                ('enabled', models.BooleanField(default=False)),
                ('schema_version', models.PositiveIntegerField(default=1)),
                ('settings', models.JSONField(blank=True, default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('variant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='OpenBench.variant')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='EngineMaintainer',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('engine', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='maintainers', to='OpenBench.engineconfig')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddField(
            model_name='engineconfig',
            name='variants',
            field=models.ManyToManyField(related_name='engines', to='OpenBench.variant'),
        ),
        migrations.CreateModel(
            name='Credential',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ciphertext', models.TextField()),
                ('updated', models.DateTimeField(auto_now=True)),
                ('engine', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='credential', to='OpenBench.engineconfig')),
            ],
        ),
        migrations.CreateModel(
            name='ConfigurationRevision',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('generation', models.PositiveBigIntegerField(unique=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('summary', models.CharField(max_length=256)),
                ('snapshot', models.JSONField()),
                ('fingerprint', models.CharField(max_length=64)),
                ('eligibility_fingerprint', models.CharField(max_length=64)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
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
            model_name='enginemaintainer',
            constraint=models.UniqueConstraint(fields=('engine', 'user'), name='unique_engine_maintainer'),
        ),
    ]
