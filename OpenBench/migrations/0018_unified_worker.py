from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0017_nnue_training')]
    operations = [migrations.AddField(
        model_name='trainingworker',
        name='machine',
        field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='training_capability', to='OpenBench.machine'),
    )]
