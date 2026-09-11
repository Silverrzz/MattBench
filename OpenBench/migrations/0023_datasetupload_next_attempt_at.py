from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0022_worker_testing_only')]

    operations = [
        migrations.AddField(
            model_name='datasetupload',
            name='next_attempt_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
