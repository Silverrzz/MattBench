from django.db import migrations, models
import django.db.models.deletion


def seed_history(apps, schema_editor):
    Test = apps.get_model('OpenBench', 'Test')
    History = apps.get_model('OpenBench', 'LLRHistory')
    database = schema_editor.connection.alias
    batch = []
    tests = Test.objects.using(database).filter(test_mode='SPRT').only('id', 'games', 'currentllr')
    for test in tests.iterator(chunk_size=1000):
        test.llr_history_state = {'count': 1, 'last_games': test.games}
        batch.append(test)
        if len(batch) == 1000:
            History.objects.using(database).bulk_create([
                History(test_id=item.id, games=item.games, llr=item.currentllr) for item in batch
            ])
            Test.objects.using(database).bulk_update(batch, ['llr_history_state'])
            batch = []
    if batch:
        History.objects.using(database).bulk_create([
            History(test_id=item.id, games=item.games, llr=item.currentllr) for item in batch
        ])
        Test.objects.using(database).bulk_update(batch, ['llr_history_state'])


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0016_configuration_foundation')]
    replaces = [
        ('OpenBench', '0017_llr_history'),
        ('OpenBench', '0018_bound_llr_history'),
        ('OpenBench', '0019_gap_sample_llr_history'),
    ]
    operations = [
        migrations.CreateModel(
            name='LLRHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('games', models.IntegerField()),
                ('llr', models.FloatField()),
                ('test', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='llr_history', to='OpenBench.test')),
            ],
            options={'ordering': ['games']},
        ),
        migrations.AddConstraint(
            model_name='llrhistory',
            constraint=models.UniqueConstraint(fields=('test', 'games'), name='unique_test_llr_games'),
        ),
        migrations.AddField(
            model_name='test',
            name='llr_history_state',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(seed_history, migrations.RunPython.noop),
    ]
