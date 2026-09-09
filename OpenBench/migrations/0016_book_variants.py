from django.db import migrations, models


def copy_variants(apps, schema_editor):
    Book = apps.get_model('OpenBench', 'OpeningBook')
    database = schema_editor.connection.alias
    for book in Book.objects.using(database).exclude(variant_id=None).iterator():
        book.variants.add(book.variant_id)


def restore_variant(apps, schema_editor):
    Book = apps.get_model('OpenBench', 'OpeningBook')
    database = schema_editor.connection.alias
    for book in Book.objects.using(database).prefetch_related('variants').iterator():
        variant = book.variants.order_by('name').first()
        Book.objects.using(database).filter(pk=book.pk).update(variant_id=variant.pk if variant else None)


class Migration(migrations.Migration):
    dependencies = [('OpenBench', '0015_configuration_and_workload_tracking')]
    operations = [
        migrations.AddField(
            model_name='openingbook', name='variants',
            field=models.ManyToManyField(blank=True, related_name='books', to='OpenBench.variant'),
        ),
        migrations.RunPython(copy_variants, restore_variant),
        migrations.RemoveField(model_name='openingbook', name='variant'),
    ]
