import json
from pathlib import Path
from string import Template


def generate(spec, profile):
    from OpenBench.schedule_builder import MANIFEST, MANIFEST_VERSION, SOURCE, assets, dataset_stages, fingerprint, lr_scheduler
    from OpenBench.training import validate_schedule
    from OpenBench.training_profiles import profile_runtime, profile_sources
    text = Template((Path(__file__).parent / 'data/duck_main.rs').read_text()).substitute(
        base_inputs=('ChessBucketsMirrored::new([' + ', '.join(str(spec['king_layout'][rank * 8 + file]) for rank in range(8) for file in range(4)) + '])') if spec['mirrored'] else ('ChessBuckets::new([' + ', '.join(map(str, spec['king_layout'])) + '])'),
        threats=str(spec['threat_inputs']).lower(), pairs=str(spec['pawn_pair_inputs']).lower(), clock=str(spec['half_move_clock']).lower(), merged=str(spec['merged_king_planes']).lower(), psqt=str(spec['psqt_inputs']).lower(),
        l1=spec['layers'][0], l2=spec['layers'][1], l3=spec['layers'][2], scale=repr(spec['eval_scale']),
        duck=str(spec['duck_inputs']).lower(), mirrored=str(spec['mirrored']).lower(),
        superbatches=spec['superbatches'], dataset_ranges=', '.join('(%d, %d)' % (row['start'], row['end']) for row in dataset_stages(spec)),
        buffer=spec['buffer_mb'], threads=spec['threads'], quiet=str(spec['filter_tactical']).lower(), max_eval=spec['max_eval'],
        batch_size=spec['batch_size'], batches=spec['batches_per_superbatch'], save_every=spec['save_every'],
        lr_scheduler=lr_scheduler(spec), wdl_rows=', '.join('(%d, %d, %s, %s, %d)' % (stage['start'], stage['end'], repr(float(stage['initial'])), repr(float(stage['final'])), ('constant', 'linear', 'cosine').index(stage['kind'])) for stage in spec['wdl_stages']),
    )
    files = {SOURCE: text, 'LICENSE.txt': assets()[3], 'examples/feature_inputs.rs': assets()[1], 'examples/mattbench_wak_loader.rs': Template((Path(__file__).parent / 'data/duck_loader.rs').read_text()).substitute(seed=spec['seed'])}
    settings = profile_runtime(profile, spec, resume_supported=True)
    profile_sources(files, settings)
    features = ((704 if spec['merged_king_planes'] else 768) * spec['psqt_inputs'] + 11 * spec['half_move_clock']) * spec['input_buckets'] + 4560 * spec['pawn_pair_inputs'] + (59808 if spec['pawn_pair_inputs'] else 60144) * spec['threat_inputs'] + 64 * spec['duck_inputs']
    l1, l2, l3 = spec['layers']
    size = (features * l1 + l1) * 2 + l1 * l2 + l2 * 4 + 2 * l2 * l3 * 4 + l3 * 4 + l3 * 4 + 4
    files[MANIFEST] = json.dumps({'version': MANIFEST_VERSION, 'spec': spec, 'training_profile': profile,
        'fingerprint': fingerprint(files, settings), 'workload_bounds': True,
        'export': {'mode': 'wakwak', 'input_features': features, 'duck_inputs': spec['duck_inputs'], 'layers': spec['layers'],
                   'mirrored': spec['mirrored'], 'byte_order': 'little', 'network_bytes': (size + 63) // 64 * 64,
                   'layout': 'wakwak-pairwise-dual-v1', 'quantisation': [255, 128, 64], 'padding': 64}}, indent=2) + '\n'
    return spec, files, validate_schedule(files, settings)
