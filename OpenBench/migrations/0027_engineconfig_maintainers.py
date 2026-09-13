from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('OpenBench', '0026_discord_notifications'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='engineconfig',
            name='maintainers',
            field=models.ManyToManyField(blank=True, related_name='maintained_engines', to=settings.AUTH_USER_MODEL),
        ),
    ]
