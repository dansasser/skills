"""Versioned adaptive-paint settings; validation requires only the standard library."""
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from numbers import Integral, Real

from . import limits


@dataclass(frozen=True)
class PaintConfig:
    schema_version: int = 1
    ratio: float = 0.55
    max_width_fraction: float = 0.055
    min_width: float = 1.2
    max_tiers: int = 12
    batch_size: int = 4096
    max_batches: int = 2048
    evaluation_pixels: int = 131072
    target_error: float = 0.020
    low_error: float = 0.006
    texture_tolerance: float = 0.25
    min_gain: float = 0.000025
    relative_gain: float = 0.001
    patience: int = 4
    min_coverage: float = 0.97
    uniform_fraction: float = 0.25
    rejection_tolerance: float = 0.0001

    def as_dict(self):
        return asdict(self)


def resolve_config(value=None, finish='photographic'):
    if finish not in ('photographic', 'painterly'):
        raise ValueError('finish must be photographic or painterly')
    if isinstance(value, PaintConfig):
        value = value.as_dict()
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError('paint config must be a JSON object')
    unknown = set(value)-set(PaintConfig.__dataclass_fields__)
    if unknown:
        raise ValueError('unknown paint config fields: '+', '.join(sorted(unknown)))
    settings = PaintConfig()
    if finish == 'painterly':
        settings = replace(settings, min_width=2.8, target_error=0.030, low_error=0.025)
    settings = replace(settings, **value)
    ints = {'schema_version': (1, 1), 'max_tiers': (1, 32), 'batch_size': (1, 65536),
            'max_batches': (1, 8192), 'evaluation_pixels': (64, 262144), 'patience': (1, 64)}
    ranges = {'ratio': (0.1, 0.9), 'max_width_fraction': (.005, .3), 'min_width': (.5, 64),
              'target_error': (0, 1), 'min_gain': (0, 1), 'relative_gain': (0, 1),
              'low_error': (0, 1), 'texture_tolerance': (0, 1),
              'min_coverage': (.1, 1), 'uniform_fraction': (.05, 1),
              'rejection_tolerance': (0, .1)}
    for key, (lo, hi) in ints.items():
        v = getattr(settings, key)
        if isinstance(v, bool) or not isinstance(v, Integral) or not lo <= v <= hi:
            raise ValueError(f'paint config {key} must be an integer in {lo}..{hi}')
    for key, (lo, hi) in ranges.items():
        v = getattr(settings, key)
        if isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f'paint config {key} must be finite in {lo}..{hi}')
    return settings


def config_digest(config, finish):
    return hashlib.sha256(json.dumps({'policy': 'adaptive-v1', 'finish': finish,
                                      'config': config.as_dict()}, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def stroke_ceiling(width, height, max_strokes=None):
    for name, v in [('width', width), ('height', height)]:
        if isinstance(v, bool) or not isinstance(v, Integral) or v <= 0:
            raise ValueError(f'{name} must be a positive integer')
    lv = limits.active().level
    admitted = lv.strokes * min(int(width)*int(height), lv.pixels) // lv.pixels
    if max_strokes is not None:
        if (isinstance(max_strokes, bool) or not isinstance(max_strokes, Integral)
                or not 0 < max_strokes <= admitted):
            raise ValueError(f'max_strokes can only lower the area-based ceiling {admitted}')
        admitted = int(max_strokes)
    if admitted < 1:
        raise ValueError('the image area admits no useful stroke within this profile')
    return admitted


@dataclass(frozen=True)
class BrushTier:
    id: int
    width: tuple
    length: tuple
    opacity: float
    bristle: float
    pickup_blur: float
    colour_jitter: float
    perspective: bool


def brush_tiers(width, height, scale, config, finish):
    floor = config.min_width * (max(1.0, scale) if finish == 'painterly' else 1.0)
    largest = max(floor, min(width, height)*config.max_width_fraction)
    tiers = []
    current = largest
    while len(tiers) < config.max_tiers:
        coarse = current > 6
        tiers.append(BrushTier(len(tiers), (current, current*1.5),
                               (current*2.8, current*6), .92,
                               .25 if coarse else .12, max(0, current*.18-.25),
                               .010 if coarse else .004, coarse))
        if current <= floor:
            break
        current = max(floor, current*config.ratio)
    return tuple(tiers)
