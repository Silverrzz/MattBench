def stage_sources(stage):
    return stage.get('sources') or [stage]


def registry_selections(dataset):
    return [[source.get('registry_id', '') for source in stage_sources(stage)]
            for stage in (dataset.get('stages') or [dataset])]


def has_multiple_sources(dataset):
    return any(len(stage_sources(stage)) > 1 for stage in (dataset.get('stages') or [dataset]))


def dataset_revisions(dataset):
    revisions = dict.fromkeys(
        (source.get('repo', ''), source.get('commit', ''))
        for stage in (dataset.get('stages') or [dataset])
        for source in stage_sources(stage)
        if source.get('repo') or source.get('commit'))
    return [{'repo': repo, 'commit': commit} for repo, commit in revisions]
