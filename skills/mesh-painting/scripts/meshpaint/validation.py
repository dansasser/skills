"""Small shared public-data checks, before conversion, counters or native calls."""
from numbers import Integral
import numpy as np


def integer(value, field, minimum=0, maximum=2**31-1):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral) or not minimum <= int(value) <= maximum:
        raise ValueError(f'{field} must be an integer in {minimum}..{maximum}; got {value!r}')
    return int(value)


def finite(value, field, shape=None, positive=False, nonnegative=False):
    try:
        a = np.asarray(value)
        valid = a.dtype.kind in 'iuf' and np.isfinite(a).all()
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid or (shape is not None and a.shape != shape):
        raise ValueError(f'{field} must contain finite numbers' + (f' of shape {shape}' if shape is not None else ''))
    if positive and not (a > 0).all():
        raise ValueError(f'{field} must be positive; got {value!r}')
    if nonnegative and not (a >= 0).all():
        raise ValueError(f'{field} must be nonnegative; got {value!r}')
    return a


def indices(value, field, shape=None, maximum=2**31-1):
    a = np.asarray(value)
    if a.dtype.kind not in 'iu' or (shape is not None and a.shape != shape):
        raise ValueError(f'{field} must be an integer array' + (f' of shape {shape}' if shape else ''))
    if a.size and (np.any(a < 0) or np.any(a > maximum)):
        raise ValueError(f'{field} contains an index outside 0..{maximum}')
    return a


def mesh_arrays(V, F, field='mesh'):
    V = finite(V, field+'.vertices')
    if V.ndim != 2 or V.shape[1] != 3 or len(V) == 0:
        raise ValueError(f'{field}.vertices must be a nonempty (N, 3) array')
    F = indices(F, field+'.faces')
    if F.ndim != 2 or F.shape[1] != 3:
        raise ValueError(f'{field}.faces must be an (M, 3) integer array')
    if F.size and int(F.max()) >= len(V):
        raise ValueError(f'{field}.faces must reference vertices 0..{len(V)-1}')
    return V, F
