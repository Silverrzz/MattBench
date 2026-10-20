from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0029_training_configuration')]

    operations = [
        migrations.AlterField(
            model_name='test',
            name='info',
            field=models.TextField(blank=True, default=''),
        ),
    ]
