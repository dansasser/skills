"""Hard resource limits. Standard library only, so it can be set up before anything heavy loads.

A level caps four counts and two process figures:

    vertices, triangles   total in the scene's meshes, checked while the scene is being built
    pixels                canvas width x height after --scale
    strokes               brushstrokes across all passes
    memory_mb             ceiling on the renderer process's address space
    seconds               wall-clock limit on the renderer process

Counts are checked before the allocation or work that would cross them. The two process figures
are put on the renderer process before it loads the scene file. Exceeding any of them raises
LimitExceeded; nothing is retried at a lower level and nothing about the scene is changed.

What "before" means for the mesh counts: each helper checks the scene's committed total plus its
own request before it builds anything, and MeshSet.add checks before it converts or counts a mesh.
Meshes a scene has built but not yet added are not counted; the memory ceiling bounds those.

These checks guard against jobs that are too large, not against a scene file that sets out to
defeat them: scene files are Python and run inside the renderer. The memory ceiling (a hard
rlimit) and the supervisor's kill after the time limit hold whatever the scene file does.

The public resource table and recovery guidance are in references/limits.md.
"""
import os
import signal
import time
from collections import namedtuple

from . import procfs

Level = namedtuple("Level", "name scale vertices triangles pixels strokes memory_mb seconds")

LEVELS = {
    "draft": Level("draft", 1.2, 1_000_000, 1_000_000, 2_304_000, 750_000, 2048, 120),
    "standard": Level("standard", 2.4, 4_000_000, 4_000_000, 9_216_000, 3_000_000, 5120, 600),
    "high": Level("high", 3.6, 8_000_000, 8_000_000, 20_736_000, 6_750_000, 10240, 1800),
}
DEFAULT_LEVEL = "standard"
EXIT_LIMIT = 3            # exit code of a run stopped by a limit
KILL_GRACE = 20.0         # seconds the supervisor waits after the time limit before killing the renderer
MIN_MEMORY_MB = 768       # below this the engine's own libraries cannot load, so a lower ceiling is refused
REARM = 5.0               # the time limit fires again this often, in case the first one was swallowed

UNITS = {"vertices": "vertices", "triangles": "triangles", "pixels": "pixels", "strokes": "brushstrokes",
         "memory": "MiB", "time": "s"}


class LimitExceeded(BaseException):
    """A job asked for, or used, more than its level allows.
    Not an Exception subclass, so a scene file's `except Exception` does not swallow it."""

    def __init__(self, stage, limit, allowed, requested=None, used=None, level=None, detail=""):
        self.stage, self.limit, self.allowed = stage, limit, allowed
        self.requested, self.used, self.detail = requested, used, detail
        self.level = level or _active.level.name
        super().__init__(self.text())
        if _active.first_stop is None:               # remembered even if it is raised in a thread or swallowed
            _active.first_stop = self

    def as_dict(self):
        return dict(stage=self.stage, limit=self.limit, unit=UNITS[self.limit], requested=self.requested,
                    used=self.used, allowed=self.allowed, level=self.level, detail=self.detail)

    def text(self):
        return describe(self.as_dict())


def _n(v):
    if v is None:
        return "not known"
    return f"{v:,.1f}" if isinstance(v, float) else f"{v:,}"


def describe(d):
    """One line: which stage hit which limit, what was requested or used, what is allowed."""
    parts = []
    if d.get("requested") is not None:
        parts.append(f"requested {_n(d['requested'])} {d['unit']}")
    if d.get("used") is not None:
        parts.append(f"used {_n(d['used'])} {d['unit']}")
    line = (f"stage '{d['stage']}' hit the {d['limit']} limit of level '{d['level']}': "
            f"{', '.join(parts) or 'amount not known'}; allowed {_n(d['allowed'])} {d['unit']}")
    return line + (f" ({d['detail']})" if d.get("detail") else "")


class Budget:
    """The active level and what the job has used of it so far."""

    def __init__(self, level=DEFAULT_LEVEL, memory_mb=None, seconds=None):
        base = LEVELS[level]
        for name, v in (("memory_mb", memory_mb), ("seconds", seconds)):
            if v is not None and not 0 < v <= getattr(base, name):
                raise ValueError(f"{name} may only lower the level's own limit ({getattr(base, name)}), got {v}")
        if memory_mb is not None and memory_mb < MIN_MEMORY_MB:
            raise ValueError(f"memory_mb below {MIN_MEMORY_MB} leaves no room to load the engine, got {memory_mb}")
        self.level = base._replace(memory_mb=memory_mb or base.memory_mb, seconds=seconds or base.seconds)
        self.vertices = self.triangles = self.pixels = 0
        self.strokes_planned = self.strokes_done = 0
        self.stage = "process setup"
        self.t0 = time.monotonic()
        self.memory_enforced = False
        self.on_stage = None
        self.first_stop = None        # the first LimitExceeded created in this run
        self.timed_out = False        # the time limit has fired at least once
        self.interrupted = False      # the run has been asked to stop

    def elapsed(self):
        return time.monotonic() - self.t0

    def usage(self):
        out = dict(vertices=self.vertices, triangles=self.triangles, pixels=self.pixels,
                    strokes_planned=self.strokes_planned, strokes_done=self.strokes_done,
                    seconds=round(self.elapsed(), 1), last_stage=self.stage, timed_out=self.timed_out, **process_memory())
        for key in ('job_plan','resource_plan','projection_totals','auxiliary_maps'):
            if hasattr(self,key):
                out[key] = getattr(self,key)
        return out

    def allowed(self):
        lv = self.level
        return dict(vertices=lv.vertices, triangles=lv.triangles, pixels=lv.pixels, strokes=lv.strokes,
                    memory_mb=lv.memory_mb, seconds=lv.seconds)


_active = Budget()
_locked = False


def active():
    return _active


def use(level=DEFAULT_LEVEL, memory_mb=None, seconds=None):
    """Make `level` the active budget (counts start from zero) and return it.
    For library callers and the command line. Refused once a run has locked its level."""
    global _active
    if _locked:
        raise RuntimeError("the resource level of this run is fixed; it cannot be changed from inside the run")
    _active = Budget(level, memory_mb, seconds)
    return _active


def lock():
    """Fix the active level for the rest of this process (the renderer does this before the scene loads)."""
    global _locked
    _locked = True


def reraise_latched():
    """Raise the first limit stop of this run if it did not reach the top by itself: one raised in a
    scene's thread, or caught by the scene file."""
    if _active.first_stop is not None:
        raise _active.first_stop
    if _active.interrupted:
        raise SystemExit(143)


# Signals that arrive while finished images are being given their real names are held, in Python,
# until the renames are done. (Blocking them with a signal mask is not enough once numpy has started
# worker threads: the signal is taken on another thread and the handler still runs here.)
_hold = {"on": False, "pending": None}


def raise_or_hold(exc):
    """For signal handlers: raise `exc` now, or keep it until release() if a hold is on."""
    if _hold["on"]:
        if _hold["pending"] is None:
            _hold["pending"] = exc
        return
    raise exc


def hold():
    _hold["on"] = True


def release():
    _hold["on"] = False
    exc, _hold["pending"] = _hold["pending"], None
    if exc is not None:
        raise exc


def stage(name):
    """Record what the job is doing, so a stop can say where it happened."""
    _active.stage = name
    if _active.on_stage:
        _active.on_stage(name)


# ---- counts -------------------------------------------------------------------------------------

def check_mesh(where, vertices, triangles):
    """Call BEFORE allocating a mesh of this size. Raises if the scene total would pass the level."""
    b = _active
    for limit, add, have, cap in (("vertices", int(vertices), b.vertices, b.level.vertices),
                                  ("triangles", int(triangles), b.triangles, b.level.triangles)):
        if have + add > cap:
            raise LimitExceeded(f"scene build ({where})", limit, cap, requested=have + add, used=have,
                                detail=f"{_n(have)} already in the scene plus {_n(add)} for this call")


def commit_mesh(vertices, triangles):
    """Count a mesh that has been added to the scene. Checks first."""
    check_mesh("MeshSet.add", vertices, triangles)
    _active.vertices += int(vertices); _active.triangles += int(triangles)


def confirm_mesh(vertices, triangles):
    """Check the mesh the renderer was actually handed, in case a scene built it without MeshSet.add."""
    b = _active
    for limit, n, cap in (("vertices", int(vertices), b.level.vertices), ("triangles", int(triangles), b.level.triangles)):
        if n > cap:
            raise LimitExceeded("render (mesh totals)", limit, cap, requested=n, used=n,
                                detail="the scene's finished mesh; it was not built through MeshSet.add")
    b.vertices, b.triangles = int(vertices), int(triangles)


def check_pixels(where, width, height):
    """Call BEFORE allocating a canvas of this size."""
    b = _active
    n = int(width) * int(height)
    if n > b.level.pixels:
        raise LimitExceeded(where, "pixels", b.level.pixels, requested=n, detail=f"canvas {int(width)} x {int(height)}")
    b.pixels = max(b.pixels, n)


def check_projection(where, vertices, triangles):
    """Bound generated clipping buffers separately; scene totals stay truthful."""
    b = _active
    for name, n in [('vertices',int(vertices)),('triangles',int(triangles))]:
        cap = getattr(b.level,name)
        if n > cap:
            raise LimitExceeded(where,name,cap,requested=n,
                                detail='generated clipping buffers, without changing scene totals')
    b.projection_totals = dict(vertices=int(vertices),triangles=int(triangles))


def check_auxiliary(where, width, height, pixel_limit=None):
    b = _active
    if not isinstance(width,int) or not isinstance(height,int) or min(width,height)<1 or max(width,height)>2**31-1:
        raise ValueError(f'{where}: invalid raster dimensions {width!r} x {height!r}')
    cap = min(b.level.pixels,b.level.pixels if pixel_limit is None else int(pixel_limit))
    if width*height > cap:
        raise LimitExceeded(where,'pixels',cap,requested=width*height,detail='auxiliary raster allocation')


def check_strokes(where, planned):
    """Call BEFORE generating any strokes, with the total planned across all passes."""
    b = _active
    if int(planned) > b.level.strokes:
        raise LimitExceeded(where, "strokes", b.level.strokes, requested=int(planned), used=b.strokes_done,
                            detail="total across all passes")
    b.strokes_planned = int(planned)


# ---- process ceilings ---------------------------------------------------------------------------

def process_memory():
    """Peak and current memory of this process in kB, from Linux procfs."""
    out = {}
    try:
        with open(procfs.mount() / "self" / "status") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if k in ("VmPeak", "VmSize", "VmHWM", "VmRSS"):
                    out[{"VmPeak": "peak_address_space_kb", "VmSize": "address_space_kb",
                         "VmHWM": "peak_rss_kb", "VmRSS": "rss_kb"}[k]] = int(v.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return out


def apply_memory_ceiling():
    """Cap this process's address space at the level's ceiling. Raises LimitExceeded if the ceiling
    cannot be put in place: the engine does not run without it."""
    b = _active
    want = b.level.memory_mb * 1024 * 1024
    try:
        import resource
        for outside in resource.getrlimit(resource.RLIMIT_AS):          # soft, then hard
            if outside != resource.RLIM_INFINITY and outside < want:
                want = outside                             # something outside already set a tighter one: keep it
                b.level = b.level._replace(memory_mb=want // (1024 * 1024))
        if want < MIN_MEMORY_MB * 1024 * 1024:
            raise LimitExceeded("process setup", "memory", b.level.memory_mb, requested=MIN_MEMORY_MB,
                                detail=f"an address-space limit set outside this run leaves less than the {MIN_MEMORY_MB} MiB "
                                       "the engine needs to load")
        resource.setrlimit(resource.RLIMIT_AS, (want, want))
        got = resource.getrlimit(resource.RLIMIT_AS)[0]
    except (ImportError, ValueError, OSError) as e:
        raise LimitExceeded("process setup", "memory", b.level.memory_mb,
                            detail=f"the memory ceiling could not be set on this system ({e}); refusing to run without it")
    if got != want:
        raise LimitExceeded("process setup", "memory", b.level.memory_mb,
                            detail="the memory ceiling did not take effect on this system; refusing to run without it")
    b.memory_enforced = True
    return want


def memory_exceeded(error):
    """Turn a MemoryError into a LimitExceeded that names the stage and the amounts."""
    b = _active
    mem = process_memory()
    used = round(mem.get("address_space_kb", 0) / 1024.0, 1) if mem else None
    return LimitExceeded(b.stage, "memory", b.level.memory_mb, used=used,
                         detail=("an allocation was refused at the ceiling" + (f": {error}" if str(error) else "")))


def start_timer():
    """Start the wall-clock limit. When it runs out, LimitExceeded is raised in the main thread."""
    b = _active

    def _alarm(signum, frame):
        b.timed_out = True
        raise_or_hold(LimitExceeded(b.stage, "time", b.level.seconds, used=round(b.elapsed(), 1)))
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, max(b.level.seconds - b.elapsed(), 0.001), REARM)


def stop_timer():
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (AttributeError, ValueError, OSError):
        pass
