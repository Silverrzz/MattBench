from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0027_engineconfig_maintainers')]

    operations = [
        migrations.AddField(model_name='test', name='errors_acknowledged', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='test', name='ignore_all_errors', field=models.BooleanField(default=False)),
    ]
