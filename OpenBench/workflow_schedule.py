import copy

from OpenBench.schedule_builder import DEFAULT_SPEC


def default_spec(profile):
    spec = copy.deepcopy(DEFAULT_SPEC)
    spec['training_profile'] = profile['id']
    if profile['generator'] == 'duckchess':
        spec.update(layers=[1024, 16, 32], activation='crelu', score_buckets=1, hidden_layers_bucketed=False,
                    pairwise_activation=True, dual_activation=True, position_filtering=False, filter_check=False, loader_interleave=False)
    return spec
