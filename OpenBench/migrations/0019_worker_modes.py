from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0018_unified_worker')]
    operations = [
        migrations.AddField(model_name=name, name='mode', field=models.CharField(max_length=16, default='automatic', choices=[('automatic', 'Automatic'), ('training-only', 'Training only'), ('paused', 'Paused')]))
        for name in ('machine', 'trainingworker')
    ] + [
        migrations.AlterField(model_name='trainingrun', name='requested_worker', field=models.ForeignKey(to='OpenBench.trainingworker', on_delete=models.PROTECT, null=True, blank=True, related_name='requested_runs')),
    ]
