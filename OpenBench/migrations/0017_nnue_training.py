import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0016_book_variants'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TrainingServiceLease',
            fields=[
                ('name', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('token', models.UUIDField()),
                ('expires', models.DateTimeField()),
            ],
        ),
        migrations.AlterField(
            model_name='test',
            name='base_options',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='test',
            name='dev_options',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='test',
            name='max_games',
            field=models.BigIntegerField(default=0),
        ),
        migrations.CreateModel(
            name='HuggingFaceCredential',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account', models.CharField(max_length=128)),
                ('wrapped_key', models.TextField()),
                ('ciphertext', models.TextField()),
                ('updated', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='huggingface', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='LifecycleEvent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('key', models.CharField(max_length=200, unique=True)),
                ('kind', models.CharField(db_index=True, max_length=64)),
                ('subject_type', models.CharField(max_length=32)),
                ('subject_id', models.CharField(max_length=64)),
                ('data', models.JSONField(default=dict)),
                ('version', models.PositiveIntegerField(default=1)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.CreateModel(
            name='TrainingArtifact',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('sha256', models.CharField(max_length=64)),
                ('size', models.BigIntegerField()),
                ('path', models.CharField(max_length=256)),
                ('kind', models.CharField(default='network', max_length=16)),
                ('network', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='OpenBench.network')),
            ],
        ),
        migrations.CreateModel(
            name='TrainingCheckpoint',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('superbatch', models.PositiveIntegerField()),
                ('metadata', models.JSONField(default=dict)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
                ('archive', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='checkpoint_archive', to='OpenBench.trainingartifact')),
                ('network', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='checkpoint_network', to='OpenBench.trainingartifact')),
            ],
            options={
                'ordering': ['-superbatch'],
            },
        ),
        migrations.CreateModel(
            name='TrainingRun',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=128)),
                ('snapshot', models.JSONField(default=dict)),
                ('dataset', models.JSONField(default=dict)),
                ('parameters', models.JSONField(default=dict)),
                ('state', models.CharField(choices=[('VALIDATING', 'Checking inputs'), ('PREPARING', 'Checking inputs'), ('QUEUED', 'Waiting for worker'), ('DOWNLOADING', 'Downloading'), ('CONVERTING', 'Converting'), ('COMPILING', 'Compiling'), ('TRAINING', 'Training'), ('SAVING', 'Saving outputs'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELLED', 'Cancelled')], db_index=True, default='VALIDATING', max_length=16)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('started', models.DateTimeField(blank=True, null=True)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('metrics', models.JSONField(default=dict)),
                ('history', models.JSONField(default=list)),
                ('log_tail', models.TextField(blank=True)),
                ('error', models.TextField(blank=True)),
                ('report_sequence', models.BigIntegerField(default=0)),
                ('cancel_requested', models.BooleanField(default=False)),
                ('claim_id', models.UUIDField(blank=True, null=True, unique=True)),
                ('preparation', models.JSONField(default=dict)),
                ('task_attempts', models.PositiveIntegerField(default=0)),
                ('engine', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='OpenBench.engineconfig')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='training_runs', to=settings.AUTH_USER_MODEL)),
                ('recovery_run', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='recovered_from', to='OpenBench.trainingrun')),
                ('resume_from', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='resumed_runs', to='OpenBench.trainingcheckpoint')),
            ],
            options={
                'ordering': ['-created'],
            },
        ),
        migrations.AddField(
            model_name='trainingcheckpoint',
            name='run',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkpoints', to='OpenBench.trainingrun'),
        ),
        migrations.AddField(
            model_name='trainingartifact',
            name='run',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='artifacts', to='OpenBench.trainingrun'),
        ),
        migrations.CreateModel(
            name='TrainingSchedule',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('files', models.JSONField(default=dict)),
                ('settings', models.JSONField(default=dict)),
                ('version', models.PositiveIntegerField(default=1)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('engine', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='training_schedules', to='OpenBench.engineconfig')),
                ('owner', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['engine__name', 'name'],
            },
        ),
        migrations.AddField(
            model_name='trainingrun',
            name='schedule',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='OpenBench.trainingschedule'),
        ),
        migrations.CreateModel(
            name='TrainingWorker',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('secret_hash', models.CharField(max_length=64)),
                ('info', models.JSONField(default=dict)),
                ('updated', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('enabled', models.BooleanField(default=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='training_workers', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddField(
            model_name='trainingrun',
            name='requested_worker',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='requested_runs', to='OpenBench.trainingworker'),
        ),
        migrations.AddField(
            model_name='trainingrun',
            name='worker',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='runs', to='OpenBench.trainingworker'),
        ),
        migrations.CreateModel(
            name='DatasetUpload',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('repo', models.CharField(max_length=256)),
                ('filename', models.CharField(max_length=512)),
                ('private', models.BooleanField(default=True)),
                ('state', models.CharField(db_index=True, default='QUEUED', max_length=16)),
                ('progress', models.FloatField(default=0)),
                ('stage', models.CharField(default='Waiting to upload', max_length=64)),
                ('revision', models.CharField(blank=True, max_length=40)),
                ('sha256', models.CharField(blank=True, max_length=64)),
                ('error', models.TextField(blank=True)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated', models.DateTimeField(default=django.utils.timezone.now)),
                ('task_attempts', models.PositiveIntegerField(default=0)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('workload', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='dataset_uploads', to='OpenBench.test')),
            ],
            options={
                'ordering': ['-created'],
                'constraints': [models.UniqueConstraint(condition=models.Q(('state__in', ['QUEUED', 'UPLOADING'])), fields=('repo', 'filename'), name='one_dataset_upload_per_path')],
            },
        ),
        migrations.CreateModel(
            name='TrainingDataset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('repo', models.CharField(max_length=256)),
                ('revision', models.CharField(default='main', max_length=200)),
                ('patterns', models.TextField(default='*')),
                ('notes', models.TextField(blank=True)),
                ('metadata', models.JSONField(default=dict)),
                ('checked', models.DateTimeField(blank=True, null=True)),
                ('archived', models.BooleanField(default=False)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='training_datasets', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
                'constraints': [models.UniqueConstraint(fields=('owner', 'name'), name='unique_user_training_dataset')],
            },
        ),
        migrations.AddConstraint(
            model_name='trainingcheckpoint',
            constraint=models.UniqueConstraint(fields=('run', 'superbatch'), name='unique_training_checkpoint'),
        ),
        migrations.AddConstraint(
            model_name='trainingartifact',
            constraint=models.UniqueConstraint(fields=('run', 'name'), name='unique_training_artifact'),
        ),
        migrations.AddConstraint(
            model_name='trainingschedule',
            constraint=models.UniqueConstraint(condition=models.Q(('owner__isnull', True)), fields=('engine', 'name'), name='unique_engine_training_schedule'),
        ),
        migrations.AddConstraint(
            model_name='trainingschedule',
            constraint=models.UniqueConstraint(condition=models.Q(('owner__isnull', False)), fields=('engine', 'owner', 'name'), name='unique_user_training_schedule'),
        ),
        migrations.AddConstraint(
            model_name='trainingschedule',
            constraint=models.UniqueConstraint(condition=models.Q(('engine__isnull', True), ('owner__isnull', True)), fields=('name',), name='unique_global_training_schedule'),
        ),
        migrations.AddConstraint(
            model_name='trainingschedule',
            constraint=models.UniqueConstraint(fields=('owner', 'name'), condition=models.Q(engine__isnull=True, owner__isnull=False), name='unique_personal_training_schedule'),
        ),
        migrations.AddConstraint(
            model_name='trainingrun',
            constraint=models.UniqueConstraint(condition=models.Q(('state__in', ('DOWNLOADING', 'CONVERTING', 'COMPILING', 'TRAINING', 'SAVING'))), fields=('worker',), name='one_training_per_worker'),
        ),
    ]
