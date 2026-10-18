import copy
import math
import re

from django.core.exceptions import ValidationError


TYPES = ('dataset', 'parameters', 'network', 'checkpoint', 'metrics', 'artifact')
PORTS = {
    'datagen': ({'network': 'network?', 'parameters': 'parameters?', 'base_network': 'network?', 'base_parameters': 'parameters?'}, {'data': 'dataset', 'metrics': 'metrics'}),
    'load_data': ({}, {'data': 'dataset'}),
    'load_parameters': ({}, {'parameters': 'parameters'}),
    'load_network': ({}, {'network': 'network'}),
    'load_checkpoint': ({}, {'checkpoint': 'checkpoint', 'network': 'network'}),
    'load_artifact': ({}, {'artifact': 'artifact'}),
    'merge': ({'data': 'dataset[]'}, {'data': 'dataset'}),
    'select': ({'data': 'dataset'}, {'matched': 'dataset', 'remaining': 'dataset'}),
    'split': ({'data': 'dataset'}, {'training': 'dataset', 'validation': 'dataset', 'metrics': 'metrics'}),
    'convert': ({'data': 'dataset'}, {'data': 'dataset', 'metrics': 'metrics'}),
    'upload': ({'dataset': 'dataset'}, {'data': 'dataset'}),
    'train': ({'dataset': 'dataset?', 'checkpoint': 'checkpoint?', 'parameters': 'parameters?'}, {'network': 'network', 'checkpoint': 'checkpoint', 'metrics': 'metrics'}),
    'test': ({'network': 'network?', 'parameters': 'parameters?', 'base_network': 'network?', 'base_parameters': 'parameters?'}, {'network': 'network', 'metrics': 'metrics'}),
    'tune': ({'network': 'network?', 'parameters': 'parameters?'}, {'parameters': 'parameters', 'metrics': 'metrics'}),
    'decision': ({'source': 'metrics'}, {'metrics': 'metrics'}),
    'approval': ({}, {}),
    'task': ({'data': 'artifact[]?'}, {'artifact': 'artifact'}),
}
DEFAULT_OUTPUT = {'datagen': 'data', 'upload': 'data', 'train': 'network', 'test': 'network', 'decision': 'metrics', 'tune': 'parameters'}


def ports(node):
    inputs, outputs = copy.deepcopy(PORTS[node['kind']])
    if node['kind'] == 'task':
        for direction, target in (('inputs', inputs), ('outputs', outputs)):
            custom = node.get('config', {}).get(direction)
            if custom is None:
                continue
            if not isinstance(custom, dict) or len(custom) > 32:
                raise ValidationError('Use at most 32 named ports.')
            target.clear()
            for name, kind in custom.items():
                if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name) or not isinstance(kind, str) or kind.removesuffix('?').removesuffix('[]') not in TYPES:
                    raise ValidationError('Invalid port name or asset type.')
                if direction == 'outputs' and kind not in TYPES:
                    raise ValidationError('Output ports require a single asset type.')
                target[name] = kind
    return inputs, outputs


def references(value):
    return value if isinstance(value, list) else [value]


def ref(value, nodes, input_type):
    if isinstance(value, str):
        source = nodes.get(value)
        output = DEFAULT_OUTPUT.get(source['kind'], next(iter(ports(source)[1]), '')) if source else ''
        if source and input_type.rstrip('?').rstrip('[]') == 'metrics' and 'metrics' in ports(source)[1]:
            output = 'metrics'
        value = {'node': value, 'port': output}
    if not isinstance(value, dict) or set(value) != {'node', 'port'} or value['node'] not in nodes or value['port'] not in ports(nodes[value['node']])[1]:
        raise ValidationError('Choose an existing output port.')
    actual = ports(nodes[value['node']])[1][value['port']]
    expected = input_type.removesuffix('?').removesuffix('[]')
    if actual != expected and expected != 'artifact':
        raise ValidationError('Connect compatible asset types: %s cannot supply %s.' % (actual, expected))
    return value


def inputs(node, outputs):
    result = {}
    for name, value in node['inputs'].items():
        def resolve(item):
            asset = read(item, outputs)
            if isinstance(item, str) and not asset.get('type'):
                kind = ports(node)[0][name].removesuffix('?').removesuffix('[]')
                asset = next((entry for entry in asset.get('artifacts', {}).values() if entry.get('type') == kind), None) or legacy_asset(asset, {'dataset': 'data'}.get(kind, kind))
                if asset is None:
                    raise ValidationError('The upstream asset is unavailable.')
            return asset
        result[name] = [resolve(item) for item in value] if isinstance(value, list) else resolve(value)
    return result


def read(reference, outputs):
    if isinstance(reference, str):
        return outputs[reference]
    source = outputs[reference['node']]
    asset = source.get('artifacts', {}).get(reference['port'])
    if asset is None:
        asset = legacy_asset(source, reference['port'])
    if asset is None:
        raise ValidationError('The upstream output %s is unavailable.' % reference['port'])
    return copy.deepcopy(asset)


def legacy_asset(output, port):
    if port == 'data':
        if 'dataset' in output:
            return {'type': 'dataset', 'sources': [output['dataset']]}
        if 'test' in output:
            return {'type': 'dataset', 'sources': [{'provider': 'datagen', 'workload': output['test']}]}
    if port == 'network' and output.get('network'):
        return {'type': 'network', 'network': output['network'], 'network_name': output.get('network_name', '')}
    if port == 'metrics':
        return {'type': 'metrics', 'values': {**{key: value for key, value in output.items() if type(value) in (int, float)}, **output.get('metrics', {})}}


def publish(step, **assets):
    from OpenBench.models import WorkflowAssetPin
    for asset in assets.values():
        if asset.get('type') == 'checkpoint' and asset.get('checkpoint'):
            WorkflowAssetPin.objects.get_or_create(run=step.run, checkpoint_id=asset['checkpoint'])
        for file in asset.get('files', []):
            if file.get('provider') == 'training':
                WorkflowAssetPin.objects.get_or_create(run=step.run, artifact_id=file['artifact'])
    step.outputs = {**step.outputs, 'artifacts': {**step.outputs.get('artifacts', {}), **assets}}


def parameter_values(value):
    if not isinstance(value, dict) or not value or len(value) > 4096:
        raise ValidationError('Supply a nonempty parameter object.')
    for key, number in value.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}', key) or type(number) not in (int, float) or not math.isfinite(number):
            raise ValidationError('Parameters require simple names and finite numeric values.')
    return value


def apply_parameters(options, asset):
    values = parameter_values(asset['values'])
    remaining = options
    for key in values:
        remaining = re.sub(r'(?<!\S)' + re.escape(key) + r'=[^\s]*(?:\s+|$)', '', remaining)
    return ' '.join([remaining.strip(), *('%s=%s' % (key, value) for key, value in values.items())]).strip()
