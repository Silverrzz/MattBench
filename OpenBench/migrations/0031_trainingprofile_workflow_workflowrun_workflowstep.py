import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0030_bullet_revision_validation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TrainingProfile',
            fields=[
                ('id', models.SlugField(max_length=64, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('version', models.PositiveIntegerField(default=1)),
                ('enabled', models.BooleanField(default=True)),
                ('settings', models.JSONField(default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='Workflow',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=128)),
                ('version', models.PositiveIntegerField(default=1)),
                ('definition', models.JSONField(default=dict)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('archived', models.BooleanField(default=False)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated'],
            },
        ),
        migrations.CreateModel(
            name='WorkflowRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('version', models.PositiveIntegerField()),
                ('snapshot', models.JSONField(default=dict)),
                ('state', models.CharField(db_index=True, default='RUNNING', max_length=16)),
                ('message', models.TextField(blank=True)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('workflow', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='runs', to='OpenBench.workflow')),
            ],
            options={
                'ordering': ['-created'],
            },
        ),
        migrations.CreateModel(
            name='WorkflowStep',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.SlugField(max_length=64)),
                ('state', models.CharField(db_index=True, default='PENDING', max_length=16)),
                ('outcome', models.CharField(blank=True, max_length=16)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('history', models.JSONField(default=list)),
                ('outputs', models.JSONField(default=dict)),
                ('message', models.TextField(blank=True)),
                ('started', models.DateTimeField(blank=True, null=True)),
                ('updated', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steps', to='OpenBench.workflowrun')),
                ('test', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.test')),
                ('training', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.trainingrun')),
                ('upload', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.datasetupload')),
            ],
            options={
                'ordering': ['id'],
                'constraints': [models.UniqueConstraint(fields=('run', 'key'), name='unique_workflow_step')],
            },
        ),
    ]
