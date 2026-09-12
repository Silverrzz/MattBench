"""Validation shared by builder generation and import tests."""
import math
import re

from django.core.exceptions import ValidationError


def parse_array(text):
    if not isinstance(text, str) or len(text) > 65536:
        raise ValidationError('Paste a numeric array.')
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S).strip()
    # Permit a complete Rust declaration, but never execute or partially parse it.
    if '=' in text:
        prefix, text = text.split('=', 1)
        if not re.fullmatch(r'\s*(?:(?:pub\s+)?const|static|let)\s+[A-Za-z_]\w*\s*(?::[^=]+)?\s*', prefix):
            raise ValidationError('Invalid array declaration.')
        text = text.strip()
    text = text.removesuffix(';').strip()
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1].strip()
    tokens = re.split(r'[\s,]+', text.rstrip(',').strip())
    number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
    if not tokens or any(not re.fullmatch(number, token) for token in tokens):
        raise ValidationError('The entire array must contain only numbers, separators and comments.')
    values = [float(token) for token in tokens]
    if any(not math.isfinite(value) for value in values):
        raise ValidationError('Array values must be finite.')
    return values


def import_layout(text, order='a1', mirrored=True):
    values = parse_array(text)
    if order not in ('a1', 'a8') or type(mirrored) is not bool or len(values) not in (32, 64):
        raise ValidationError('Choose an orientation and paste 32 or 64 bucket IDs.')
    if any(not value.is_integer() or not 0 <= value < 64 for value in values):
        raise ValidationError('Bucket IDs must be integers from 0 to 63.')
    values = list(map(int, values))
    width = len(values) // 8
    rows = [values[i:i + width] for i in range(0, len(values), width)]
    if width == 4:
        rows = [row + row[::-1] for row in rows]
        mirrored = True
    if order == 'a8':
        rows.reverse()
    if mirrored and any(row != row[::-1] for row in rows):
        raise ValidationError('The 64-square layout is asymmetric; choose unmirrored or correct it.')
    count = max(values) + 1
    if set(values) != set(range(count)):
        raise ValidationError('Bucket IDs must be contiguous, starting at zero.')
    return {'king_layout': sum(rows, []), 'input_buckets': count, 'mirrored': mirrored}


def validate_wdl_model(spec):
    for key in ('wdl_model_params_a', 'wdl_model_params_b'):
        values = spec[key]
        if not isinstance(values, list) or len(values) != 4 or any(type(x) not in (float, int) or not math.isfinite(x) for x in values):
            raise ValidationError('Each WDL model polynomial requires four finite coefficients.')
    scale = spec['wdl_heuristic_scale']
    if type(scale) not in (float, int) or not math.isfinite(scale) or scale <= 0:
        raise ValidationError('WDL heuristic scale must be finite and positive.')
    if spec['material_min'] > spec['material_max']:
        raise ValidationError('Minimum material cannot exceed maximum material.')
    if not spec['wdl_filtered']:
        return
    low, high = [spec[key] / spec['mom_target'] for key in ('material_min', 'material_max')]
    # Check extrema as well as endpoints: positivity at integer material counts
    # alone would miss an invalid denominator between them.
    for key in ('wdl_model_params_a', 'wdl_model_params_b'):
        a, b, c, d = spec[key]
        points = [low, high]
        normalizer = max(abs(a), abs(b), abs(c), 1)
        aa, bb, cc = a / normalizer, b / normalizer, c / normalizer
        discriminant = bb * bb - 3 * aa * cc
        if aa and discriminant >= 0:
            points.extend((-bb + sign * math.sqrt(discriminant)) / (3 * aa) for sign in (-1, 1))
        elif bb and not aa:
            points.append(-cc / (2 * bb))
        values = [((a * x + b) * x + c) * x + d for x in points if low <= x <= high]
        if any(not math.isfinite(value) or not math.isfinite(value * scale) for value in values):
            raise ValidationError('The WDL model overflows within the material range.')
        if key.endswith('_b') and min(values) <= 0:
            raise ValidationError('The WDL model denominator must remain positive throughout the material range.')
