from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0019_worker_modes')]
    operations = [
        migrations.AddField(model_name='trainingrun', name='deleted', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='trainingworker', name='accept_any_owner', field=models.BooleanField(default=False)),
    ]
