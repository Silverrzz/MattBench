DEFAULT_BULLET_COMMIT = '1896c0884c7ededb1868068bbf5583d1726b37f6'
BULLET_REPOSITORY = 'https://github.com/jw1912/bullet'


def pinned_bullet_commit():
    from OpenBench.models import TrainingConfiguration
    return TrainingConfiguration.objects.filter(pk=1).values_list('bullet_commit', flat=True).first() or DEFAULT_BULLET_COMMIT
