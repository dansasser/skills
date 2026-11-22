"""Triangle meshes: Z-up, metres. Vertices, triangles, normals, one material id per triangle."""
import numpy as np
from . import limits
from .util import norm, scene_rng


class MeshSet:
    def __init__(self):
        self.V, self.F, self.N, self.M, self.A = [], [], [], [], []
        self.n = 0

    def add(self, V, F, mat, orient="up", attr=None):
        """mat: one id or one per triangle. orient: 'up', or a point the normals should face away from.
        attr: optional per-vertex number handed to the material shader (e.g. height fraction)."""
        limits.check_mesh("MeshSet.add", len(V), len(F))   # before anything is converted or built
        V = np.asarray(V, np.float32); F = np.asarray(F, np.int64)
        limits.commit_mesh(len(V), len(F))
        if not np.isfinite(V).all():
            raise ValueError("MeshSet.add: the mesh has vertices that are not finite numbers")
        fn = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
        vn = np.zeros_like(V)
        for k in range(3):
            np.add.at(vn, F[:, k], fn)
        vn = norm(vn)
        if isinstance(orient, str):
            vn[vn[:, 2] < 0] *= -1
        else:
            vn[(vn * (V - np.asarray(orient, np.float32))).sum(1) < 0] *= -1
        self.V.append(V); self.F.append(F + self.n); self.N.append(vn)
        self.M.append(np.broadcast_to(np.asarray(mat, np.int32), (len(F),)).copy())
        self.A.append(np.zeros(len(V), np.float32) if attr is None else np.asarray(attr, np.float32))
        self.n += len(V)

    def done(self):
        return (np.concatenate(self.V), np.concatenate(self.F).astype(np.int32), np.concatenate(self.N),
                np.concatenate(self.M), np.concatenate(self.A))


def grid_mesh(xs, ys, height_fn, drop_below=-0.4):
    """Terrain patch from a height function h(x, y). Triangles wholly under `drop_below` are removed.
    The resource level is checked against the full grid, before any are removed."""
    nx, ny = int(np.size(xs)), int(np.size(ys))
    limits.check_mesh("grid_mesh", nx * ny, 2 * max(nx - 1, 0) * max(ny - 1, 0))   # before the grid exists
    X, Y = np.meshgrid(xs, ys)
    Z = height_fn(X, Y)
    if not np.isfinite(Z).all():
        bad = int((~np.isfinite(Z)).sum())
        raise ValueError(f"grid_mesh: the height function returned {bad} values that are not finite numbers. "
                         "A common cause is a root or power of a slightly negative number; clip it first, "
                         "for example np.sqrt(np.clip(v, 0, 1)).")
    V = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1)
    ny, nx = X.shape
    i = (np.arange(ny - 1)[:, None] * nx + np.arange(nx - 1)[None]).ravel()
    F = np.concatenate([np.stack([i, i + 1, i + nx], 1), np.stack([i + 1, i + nx + 1, i + nx], 1)])
    if drop_below is not None:
        F = F[(V[F][:, :, 2] > drop_below).any(1)]
    return V, F


def lathe(cx, cy, profile, seg=56):
    """Surface of revolution about a vertical axis. profile = [(radius, z, material)];
    each material applies to the band above its point. Returns V, F, per-triangle materials."""
    limits.check_mesh("lathe", len(profile) * int(seg), 2 * int(seg) * max(len(profile) - 1, 0))
    ang = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    V = np.concatenate([np.stack([cx + r * np.cos(ang), cy + r * np.sin(ang), np.full(seg, z)], 1)
                        for r, z, _ in profile])
    F, M = [], []
    for k in range(len(profile) - 1):
        a = k * seg + np.arange(seg); b = k * seg + (np.arange(seg) + 1) % seg
        F += [np.stack([a, b, a + seg], 1), np.stack([b, b + seg, a + seg], 1)]
        M += [np.full(2 * seg, profile[k][2])]
    return V, np.concatenate(F), np.concatenate(M)


def quad(p0, p1, p2, p3):
    return np.array([p0, p1, p2, p3], np.float32), np.array([[0, 1, 2], [0, 2, 3]])


def tri(p0, p1, p2):
    return np.array([p0, p1, p2], np.float32), np.array([[0, 1, 2]])


def scatter(points, V, F, scale=1.0, yaw=None, jitter=0.0, rng=None):
    """Place one small template mesh at many points and return a single combined (V, F).

    points: (n, 3) positions for the template's origin (usually on the ground).
    V, F:   the template, modelled around the origin with its base at z = 0.
    scale:  one number, or one per point.
    yaw:    rotation about the vertical in radians, one per point; None picks random turns.
    jitter: random offset of every vertex, as a fraction of that instance's scale. This is
            what stops a field of copies looking like copies.
    rng:    a numpy Generator; None uses scene_rng("scatter"), so the result is repeatable.

    All instances share the one `orient` you pass to MeshSet.add (use "up" for ground cover),
    so do not list a scattered material in Scene.no_flip.
    """
    points = np.asarray(points, np.float32).reshape(-1, 3)
    V = np.asarray(V, np.float32); F = np.asarray(F, np.int64)
    n, m = len(points), len(V)
    limits.check_mesh("scatter", n * m, n * len(F))   # before the copies are made
    rng = rng or scene_rng("scatter")
    sc = np.broadcast_to(np.asarray(scale, np.float32), (n,)).reshape(n, 1, 1)
    yw = rng.uniform(0, 2 * np.pi, n) if yaw is None else np.broadcast_to(np.asarray(yaw, np.float64), (n,))
    c, s = np.cos(yw).astype(np.float32)[:, None], np.sin(yw).astype(np.float32)[:, None]
    x = V[None, :, 0] * c - V[None, :, 1] * s
    y = V[None, :, 0] * s + V[None, :, 1] * c
    out = np.stack([x, y, np.broadcast_to(V[None, :, 2], (n, m))], 2) * sc
    if jitter:
        out = out + rng.normal(0, 1, (n, m, 3)).astype(np.float32) * (jitter * sc)
    out = out + points[:, None, :]
    faces = (F[None, :, :] + (np.arange(n, dtype=np.int64) * m)[:, None, None]).reshape(-1, 3)
    return out.reshape(-1, 3).astype(np.float32), faces


def blades(n=9, height=0.3, width=0.03, lean=0.45, spread=0.06, rng=None):
    """A tuft of `n` thin triangular blades fanning out from a point: a template for scatter().

    Grass, reeds, heather and twiggy scrub read far better as thin blades than as solid
    lumps. height, width, spread are metres; lean is how far tips tip outward (0 = upright).
    Returns (V, F) with the base at z = 0.
    """
    limits.check_mesh("blades", 3 * int(n), int(n))
    rng = rng or scene_rng("blades")
    a = rng.uniform(0, 2 * np.pi, n)
    h = height * rng.uniform(0.55, 1.0, n)
    ln = lean * rng.uniform(0.3, 1.0, n)
    r = spread * np.sqrt(rng.uniform(0, 1, n))
    bx, by = r * np.cos(a), r * np.sin(a)                       # where each blade is rooted
    px, py = -np.sin(a) * width / 2, np.cos(a) * width / 2      # half-width, across the blade
    V = np.empty((n, 3, 3), np.float32)
    V[:, 0] = np.stack([bx - px, by - py, np.zeros(n)], 1)
    V[:, 1] = np.stack([bx + px, by + py, np.zeros(n)], 1)
    V[:, 2] = np.stack([bx + np.cos(a) * ln * h, by + np.sin(a) * ln * h, h], 1)
    return V.reshape(-1, 3), np.arange(3 * n).reshape(n, 3)


def beam(p0, p1, width=0.1):
    """A straight four-sided bar from p0 to p1, `width` metres across: legs, struts, rails,
    posts, masts. Returns (V, F); orient it away from its own midpoint:
    ms.add(*beam(a, b, 0.12), STEEL, tuple((np.add(a, b) / 2)))."""
    p0 = np.asarray(p0, np.float32); p1 = np.asarray(p1, np.float32)
    d = p1 - p0
    d = d / (np.linalg.norm(d) + 1e-9)
    ref = np.array((0, 0, 1), np.float32) if abs(d[2]) < 0.9 else np.array((1, 0, 0), np.float32)
    a = np.cross(d, ref); a = a / np.linalg.norm(a) * (width / 2)
    b = np.cross(d, a); b = b / np.linalg.norm(b) * (width / 2)
    ring = [a + b, a - b, -a - b, -a + b]
    V = np.array([p0 + r for r in ring] + [p1 + r for r in ring], np.float32)
    F = []
    for k in range(4):
        j = (k + 1) % 4
        F += [(k, j, 4 + k), (j, 4 + j, 4 + k)]
    F += [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6)]
    return V, np.array(F)
