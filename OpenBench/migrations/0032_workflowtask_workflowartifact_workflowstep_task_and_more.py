import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0031_trainingprofile_workflow_workflowrun_workflowstep'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkflowTask',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('snapshot', models.JSONField(default=dict)),
                ('inputs', models.JSONField(default=dict)),
                ('outputs', models.JSONField(default=dict)),
                ('state', models.CharField(db_index=True, default='QUEUED', max_length=16)),
                ('claim_id', models.UUIDField(blank=True, null=True, unique=True)),
                ('report_sequence', models.PositiveIntegerField(default=0)),
                ('metrics', models.JSONField(default=dict)),
                ('log', models.TextField(blank=True)),
                ('error', models.TextField(blank=True)),
                ('cancel_requested', models.BooleanField(default=False)),
                ('created', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished', models.DateTimeField(blank=True, null=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('worker', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.trainingworker')),
            ],
        ),
        migrations.CreateModel(
            name='WorkflowArtifact',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=256)),
                ('path', models.CharField(max_length=256)),
                ('size', models.BigIntegerField()),
                ('sha256', models.CharField(max_length=64)),
                ('metadata', models.JSONField(default=dict)),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='artifacts', to='OpenBench.workflowtask')),
            ],
        ),
        migrations.AddField(
            model_name='workflowstep',
            name='task',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.workflowtask'),
        ),
        migrations.AddConstraint(
            model_name='workflowtask',
            constraint=models.UniqueConstraint(condition=models.Q(('state', 'RUNNING')), fields=('worker',), name='one_workflow_task_per_worker'),
        ),
        migrations.AddConstraint(
            model_name='workflowartifact',
            constraint=models.UniqueConstraint(fields=('task', 'name'), name='unique_workflow_artifact'),
        ),
    ]
