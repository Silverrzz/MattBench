from OpenBench.bullet import BULLET_REPOSITORY, DEFAULT_BULLET_COMMIT


INPUT_FEATURES = {
    'psqt_inputs': {'label': 'Piece-square inputs (PSQ)', 'short': 'Piece squares', 'input': True, 'buckets': True},
    'mirrored': {'label': 'Horizontal mirroring', 'short': 'Mirroring', 'input': False},
    'threat_inputs': {'label': 'Threat inputs (TI)', 'short': 'Threats', 'input': True},
    'pawn_pair_inputs': {'label': 'Pawn-pair inputs (PP)', 'short': 'Pawn pairs', 'input': True},
    'half_move_clock': {'label': 'Half-Move-Clock input', 'short': 'Half-move clock', 'input': True},
    'merged_king_planes': {'label': 'Merged king planes', 'short': 'Merged kings', 'input': False},
    'duck_inputs': {'label': 'Duck-square inputs', 'short': 'Duck square', 'input': True},
}
DATA_TOOLS = {
    'pawnocchio': {'label': 'Pawnocchio', 'formats': ['vf'], 'combiner': True,
                   'repo': 'https://github.com/JonathanHallstrom/pawnocchio', 'commit': '85df99c903ee6b90dd593a71009354699a81bcc5',
                   'combiner_defaults': {'repo': 'https://github.com/JonathanHallstrom/viriformat_combiner', 'commit': '045a3e399ca971e8153847e5f3f7e55e3e5a8103'}, 'capabilities': []},
    'wakformat': {'label': 'Wakformat', 'formats': ['wf'], 'combiner': False,
                 'repo': 'https://github.com/Silverrzz/wakformat', 'commit': '50f99df368b3753b74b2dfbd9d38822d8cd5118f', 'capabilities': ['wakformat-v1']},
}
ADAPTERS = {
    'standard': {'label': 'Bullet / Viriformat', 'format': 'vf', 'generate': None,
                 'name': 'Standard chess', 'variant': 'standard', 'bullet_repo': BULLET_REPOSITORY, 'bullet_ref': DEFAULT_BULLET_COMMIT, 'data_tool': 'pawnocchio',
                 'features': [key for key in INPUT_FEATURES if key != 'duck_inputs'], 'optimizers': ['adamw', 'ranger'],
                 'defaults': {}, 'fixed': {}, 'hidden': [], 'position_fields': None,
                 'layers': {'minimum': 1, 'maximum': 8, 'multiples': {}}, 'export_note': ''},
    'duckchess': {'label': 'Bullet / Wakformat', 'format': 'wf', 'generate': 'OpenBench.duck_builder.generate',
                  'name': 'DuckChess', 'variant': 'duck', 'bullet_repo': 'https://github.com/Silverrzz/bullet', 'bullet_ref': '6629e9bc9b205d4d0dea25a24aad4234a0b4b085', 'data_tool': 'wakformat', 'rust_minimum': '1.95.0',
                  'features': list(INPUT_FEATURES), 'optimizers': ['adamw'],
                  'defaults': {'layers': [1024, 16, 32], 'input_buckets': 1, 'king_layout': [0] * 64, 'filter_check': False},
                  'fixed': {'optimizer': 'adamw', 'score_outputs': True, 'score_buckets': 1, 'wdl_outputs': False,
                            'uncertainty_outputs': False, 'skip_connection': False, 'hidden_layers_bucketed': False,
                            'pairwise_activation': True, 'pairwise_layers': [1], 'dual_activation': True, 'dual_layers': [2],
                            'pairwise_left_activation': 'crelu', 'pairwise_right_activation': 'crelu', 'activation': 'crelu',
                            'position_filtering': False, 'piece_count_sampling': False, 'result_filtering': False,
                            'wdl_filtered': False, 'export_mode': 'legacy', 'feature_format': 'i16', 'split_features': False,
                            'loader_interleave': False, 'random_fen_skip': 0},
                  'hidden': ['wdl_outputs', 'uncertainty_outputs', 'skip_connection', 'position_filtering',
                             'piece_count_sampling', 'result_filtering', 'wdl_filtered', 'hidden_layers_bucketed',
                             'export_mode', 'split_features', 'feature_format', 'filter_check', 'filter_castling',
                             'loader_interleave', 'random_fen_skip'],
                  'position_fields': ['max_eval', 'filter_tactical'], 'layers': {'minimum': 3, 'maximum': 3, 'multiples': {'0': 4}},
                  'export_note': 'Wakwak integer export: feature i16 ×255, packed L1 i8 ×128, remaining tensors i32; padded to 64 bytes.'},
}
