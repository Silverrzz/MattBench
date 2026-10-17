from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0029_training_configuration')]

    operations = [
        migrations.AddField(model_name='trainingconfiguration', name='bullet_candidate', field=models.CharField(max_length=40, blank=True, default='')),
        migrations.AddField(model_name='trainingconfiguration', name='bullet_validation', field=models.CharField(max_length=16, blank=True, default='')),
        migrations.AddField(model_name='trainingconfiguration', name='bullet_validation_message', field=models.TextField(blank=True, default='')),
        migrations.AddField(model_name='trainingconfiguration', name='bullet_validation_base', field=models.CharField(max_length=40, blank=True, default='')),
        migrations.AddField(model_name='trainingconfiguration', name='bullet_validation_token', field=models.UUIDField(null=True, blank=True)),
    ]
