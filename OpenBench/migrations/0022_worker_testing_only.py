from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0021_training_uploading_label')]
    operations = [
        migrations.AlterField(
            model_name=model_name,
            name='mode',
            field=models.CharField(
                choices=[('automatic', 'Automatic'), ('testing-only', 'Testing only'), ('training-only', 'Training only'), ('paused', 'Paused')],
                default='automatic', max_length=16,
            ),
        )
        for model_name in ('machine', 'trainingworker')
    ]
