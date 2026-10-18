import copy
import json

from django.core.exceptions import ValidationError

from OpenBench.models import TrainingSchedule
from OpenBench.training import DEFAULT_SETTINGS, hf_token, validate_schedule
from OpenBench.training_views import TrainingForm


def prepare(node, user):
    config = node['config']
    snapshot = config['schedule_snapshot']
    parameters = config.get('training', {})
    if not isinstance(parameters, dict) or set(parameters) - set(TrainingForm.base_fields):
        raise ValidationError('Invalid training configuration.')
    schedule = TrainingSchedule(name=snapshot['name'], files=snapshot['files'], settings=snapshot['settings'])
    from OpenBench.schedule_builder import schedule_dataset_stages
    ranges = schedule_dataset_stages(schedule) or [{}]
    values = {'name': 'wf-' + node['id'], 'priority': config.get('priority', 0), 'workload_size': 0,
              'checkpoint_retention': 'latest', 'checkpoint_keep_last': DEFAULT_SETTINGS['checkpoint_keep_last'],
              'delete_uploaded_checkpoints': DEFAULT_SETTINGS['delete_uploaded_checkpoints'], 'skip_broken_games': True,
              'checkpoint': '@workflow' if node['inputs'].get('checkpoint') else '',
              'stage_datasets': [{'stage': index, 'datasets': ['@workflow']} for index in range(len(ranges))], **parameters,
              'engine': config['engine'], 'training_profile': '', 'schedule': '@workflow', 'dataset': ''}
    if isinstance(values['stage_datasets'], list):
        values['stage_datasets'] = json.dumps(values['stage_datasets'])
    form = TrainingForm(user, values, workflow_schedule=schedule, workflow_data=bool(node['inputs'].get('dataset')), workflow_checkpoint=bool(node['inputs'].get('checkpoint')))
    if not form.is_valid():
        raise ValidationError(node['name'] + ': ' + '; '.join('%s: %s' % (key, ', '.join(errors)) for key, errors in form.errors.items()))
    data = form.cleaned_data
    hf_token(user.pk)
    snapshot.update(files=data['run_files'], settings=validate_schedule(data['run_files'], data['run_settings']), environment=data['environment'])
    config['training_datasets'] = data['dataset_stages'] or [data['dataset'].snapshot()]
    config['training_options'] = {key: data[key] for key in ('name', 'priority', 'workload_size', 'wdl')}
    config['training_options'].update(checkpoint=data['checkpoint'].pk if data['checkpoint'] else None, checkpoint_input=values['checkpoint'] == '@workflow')


def launch(config, inputs, user):
    from OpenBench.models import EngineConfig, TrainingCheckpoint
    from OpenBench.schedule_builder import builder_state, generate_schedule, schedule_dataset_stages
    from OpenBench.training_checkpoints import checkpoint_provenance, validate_checkpoint_schedule
    from OpenBench.training_sources import stage_sources
    from OpenBench.workflow_assets import dataset_for_training, resolve_data
    snapshot = copy.deepcopy(config['schedule_snapshot'])
    options = config.get('training_options', {})
    graph = None
    bindings = config.get('training_datasets')
    if bindings is None:
        schedule = TrainingSchedule(files=snapshot['files'], settings=snapshot['settings'])
        bindings = [{'workflow_input': True, **bounds} for bounds in schedule_dataset_stages(schedule) or [{}]]
    stages = []
    for binding in bindings:
        sources = []
        for source in stage_sources(binding):
            if source.get('workflow_input'):
                if graph is None:
                    graph = dataset_for_training(resolve_data(inputs['dataset'], user))
                sources.extend(copy.deepcopy(stage_sources(graph)))
            else:
                sources.append(copy.deepcopy(source))
        stage = sources[0] if len(sources) == 1 else {'sources': sources}
        stages.append({**stage, **{key: binding[key] for key in ('start', 'end') if key in binding}})
    dataset = stages[0] if len(stages) == 1 and 'start' not in stages[0] else {'repo': stage_sources(stages[0])[0].get('repo', ''), 'stages': stages}
    checkpoint_id = inputs.get('checkpoint', {}).get('checkpoint') if options.get('checkpoint_input', True) else options.get('checkpoint')
    resume = None
    if checkpoint_id:
        resume = TrainingCheckpoint.objects.select_for_update().select_related('run', 'archive').get(pk=checkpoint_id)
        if resume.run.owner_id != user.pk:
            raise ValidationError('Checkpoint access denied.')
        if options.get('wdl') is not None:
            spec, current = builder_state(TrainingSchedule(files=snapshot['files'], settings=snapshot['settings']))
            if not current:
                raise ValidationError('WDL overrides require a builder schedule.')
            for stage in spec['wdl_stages']:
                if stage['end'] > resume.superbatch:
                    stage.update(kind='constant', initial=options['wdl'], final=options['wdl'])
            _, snapshot['files'], _ = generate_schedule(spec, snapshot['settings']['bullet_ref'], snapshot['settings'].get('training_profile'))
            snapshot['wdl_override'] = options['wdl']
        validate_checkpoint_schedule(resume, EngineConfig.objects.get(pk=config['engine']), snapshot['files'], snapshot['settings'])
        snapshot['resume'] = checkpoint_provenance(resume)
        snapshot['bullet_commit'] = resume.run.snapshot.get('bullet_commit')
    return snapshot, dataset, resume, options
