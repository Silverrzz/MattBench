import json
import math
import re


NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
PROGRESS = re.compile(r'superbatch\s+(\d+)\s+\[(' + NUMBER + r')%\s+\((\d+)/(\d+) batches,\s*(' + NUMBER + r') pos/sec\)\]', re.I)
SUMMARY = re.compile(r'superbatch\s+(\d+)\s*\|\s*time\s+(' + NUMBER + r')s\s*\|\s*running loss\s+(' + NUMBER + r')\s*\|\s*(' + NUMBER + r') pos/sec', re.I)


def bullet_telemetry(text, previous=None):
    metrics = dict(previous or {})
    samples = []
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    for line in text.replace('\r', '\n').splitlines():
        for label, key in (('Start Superbatch', 'start_superbatch'), ('End Superbatch', 'end_superbatch'), ('Batches / Superbatch', 'batches_per_superbatch')):
            match = re.search(re.escape(label) + r'\s*:\s*(\d+)', line)
            if match:
                metrics[key] = int(match[1])
        initial_lr = re.search(r'LR Scheduler\s*:\s*start at\s+(' + NUMBER + ')', line)
        learning_rate = re.search(r'LR (?:dropped|changed|set) to\s+(' + NUMBER + ')', line)
        if initial_lr or learning_rate:
            metrics['learning_rate'] = float((learning_rate or initial_lr)[1])
        progress = PROGRESS.search(line)
        summary = SUMMARY.search(line)
        if progress:
            superbatch, percent, batch, batches, speed = progress.groups()
            metrics.update(superbatch=int(superbatch), superbatch_progress=float(percent), batch=int(batch), batches_per_superbatch=int(batches), positions_per_second=float(speed))
        if summary:
            superbatch, seconds, loss, speed = summary.groups()
            metrics.update(superbatch=int(superbatch), superbatch_progress=100.0, superbatch_seconds=float(seconds), loss=float(loss), positions_per_second=float(speed))
            if metrics.get('batches_per_superbatch'):
                metrics['batch'] = metrics['batches_per_superbatch']
            samples.append({'step': int(superbatch), 'loss': float(loss)})
        eta = re.search(r'Estimated time remaining in training:\s*(\d+)h\s*(\d+)m\s*(\d+)s', line)
        if eta:
            metrics['remaining_seconds'] = int(eta[1]) * 3600 + int(eta[2]) * 60 + int(eta[3])
        batch_eta = re.search(r'Estimated time to end of superbatch:\s*(' + NUMBER + ')s', line)
        if batch_eta:
            metrics['superbatch_remaining_seconds'] = float(batch_eta[1])
        if progress or summary:
            start = metrics.get('start_superbatch', 1)
            end = metrics.get('end_superbatch', 0)
            batch_fraction = metrics.get('batch', 0) / max(1, metrics.get('batches_per_superbatch', 1))
            metrics['step'] = (metrics['superbatch'] - 1) * metrics.get('batches_per_superbatch', 0) + metrics.get('batch', 0)
            if end >= start:
                metrics['progress'] = max(0.0, min(100.0, 100 * (metrics['superbatch'] - start + batch_fraction) / (end - start + 1)))
        marker = line.find('MATTBENCH_METRIC ')
        if marker >= 0:
            try:
                values = json.loads(line[marker + len('MATTBENCH_METRIC '):])
                allowed = ('loss', 'validation_loss', 'step', 'superbatch', 'learning_rate', 'positions_per_second', 'progress')
                if isinstance(values, dict):
                    metrics.update({key: value for key, value in values.items() if key in allowed and type(value) in (int, float) and math.isfinite(value) and abs(value) < 1e20})
                    if 'loss' in values and 'loss' in metrics:
                        samples.append({'step': metrics.get('step', metrics.get('superbatch', 0)), 'loss': metrics['loss']})
            except (ValueError, TypeError):
                pass
    metrics = {key: value for key, value in metrics.items() if type(value) not in (int, float) or math.isfinite(value) and abs(value) <= 1e20}
    samples = [sample for sample in samples if math.isfinite(sample['loss']) and abs(sample['loss']) <= 1e20]
    return metrics, samples


def merge_loss_history(history, samples, timestamp):
    history = list(history)
    seen = {(item['step'], item['loss']) for item in history}
    for sample in samples:
        key = sample['step'], sample['loss']
        if key not in seen:
            history.append({'time': timestamp, **sample})
            seen.add(key)
    return history[-600:]
