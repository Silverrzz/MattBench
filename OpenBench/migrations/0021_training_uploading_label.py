from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0020_trainingrun_deleted')]
    operations = [
        migrations.AlterField(
            model_name='trainingrun',
            name='state',
            field=models.CharField(
                choices=[
                    ('VALIDATING', 'Checking inputs'), ('PREPARING', 'Checking inputs'),
                    ('QUEUED', 'Waiting for worker'), ('DOWNLOADING', 'Downloading'),
                    ('CONVERTING', 'Converting'), ('COMPILING', 'Compiling'),
                    ('TRAINING', 'Training'), ('SAVING', 'Uploading'),
                    ('COMPLETED', 'Completed'), ('FAILED', 'Failed'), ('CANCELLED', 'Cancelled'),
                ],
                db_index=True, default='VALIDATING', max_length=16,
            ),
        ),
    ]
