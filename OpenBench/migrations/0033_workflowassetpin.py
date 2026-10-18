import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('OpenBench', '0032_workflowtask_workflowartifact_workflowstep_task_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkflowAssetPin',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('artifact', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.trainingartifact')),
                ('checkpoint', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='OpenBench.trainingcheckpoint')),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='asset_pins', to='OpenBench.workflowrun')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('run', 'artifact'), name='workflow_training_artifact_pin'), models.UniqueConstraint(fields=('run', 'checkpoint'), name='workflow_checkpoint_pin')],
            },
        ),
    ]
