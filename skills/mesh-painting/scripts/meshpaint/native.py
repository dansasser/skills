"""Loads the compiled core (built on first use with the system C compiler).

If the compiled core cannot be built or loaded, the same functions run in plain
Python: same algorithm and, within small rounding differences, the same result,
but much slower (an estimate from earlier versions is tens of times). Set
MESHPAINT_NO_NATIVE=1 to force the Python core.
"""
import atexit
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile

import numpy as np

from . import procfs

_HERE = os.path.dirname(os.path.abspath(__file__))
_lib = None
NATIVE = False
REASON = ""          # why the compiled core is not in use, when it is not
_LOADED_FDS = []       # dlopen names must not be reused for a different file


@atexit.register
def _close_loaded_fds():
    for fd in _LOADED_FDS:
        try:
            os.close(fd)
        except OSError:
            pass


class _UnsafeCache(OSError):
    pass


def _check_directory(fd, private=False):
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode):
        raise _UnsafeCache("native cache path is not a directory")
    if private:
        if info.st_uid != os.geteuid() or mode & 0o077:
            raise _UnsafeCache("native cache directory is not private to the current user")
    elif info.st_uid not in (0, os.geteuid()) or (mode & 0o022 and not mode & stat.S_ISVTX):
        raise _UnsafeCache("native cache parent has unsafe ownership or permissions")


def _child_directory(parent, name, private=False, create=True):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        _check_directory(fd, private)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _directory(path, private=False, create=True):
    """Walk without following symlinks; retain a descriptor for the checked inode."""
    fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = os.path.abspath(path).split(os.path.sep)[1:]
        for index, name in enumerate(parts):
            if not name:
                continue
            child = _child_directory(fd, name, private and index == len(parts) - 1, create)
            os.close(fd)
            fd = child
        if not parts or parts == ['']:
            _check_directory(fd, private)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _primary_directory(cache, tag):
    fd = _directory(cache)
    try:
        for name, private in (("meshpaint", False), ("native-v2", True), (tag, True)):
            child = _child_directory(fd, name, private)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
        raise _UnsafeCache("native cache file has unsafe type, ownership, permissions or links")


def _file(directory, name, flags=os.O_RDONLY, missing_ok=False):
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _UnsafeCache("native cache file is a symlink") from error
        raise
    try:
        _check_file(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _digest(fd):
    result = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        result.update(chunk)
    return result.hexdigest()


def _descriptor_path(fd):
    return str(procfs.mount() / "self" / "fd" / str(fd))


def _load_existing(directory, tag):
    library = _file(directory, "core.so", missing_ok=True)
    receipt = None
    try:
        receipt = _file(directory, "core.json", missing_ok=True)
        if library is None or receipt is None:
            return None
        try:
            raw = os.read(receipt, 4097)
            record = json.loads(raw) if len(raw) <= 4096 else None
        except (ValueError, UnicodeError):
            return None
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("source_sha256") != tag
                or record.get("library_sha256") != _digest(library)):
            return None
        # Keep the validated inode pinned across dlopen, independent of path changes.
        # Retain the descriptor until exit so dlopen cannot alias a reused /fd name.
        pinned = os.dup(library)
        try:
            lib = ctypes.CDLL(_descriptor_path(pinned))
        except BaseException:
            os.close(pinned)
            raise
        _LOADED_FDS.append(pinned)
        try:
            lib.mp_raster
            lib.mp_strokes
        except AttributeError as error:
            raise OSError("compiled library lacks renderer functions") from error
        return lib
    finally:
        if library is not None:
            os.close(library)
        if receipt is not None:
            os.close(receipt)


def _compile(src, directory, tag):
    """Compile privately, then publish library and its digest receipt under the lock."""
    # Wrappers may close inherited descriptors before invoking the real compiler.
    # Anchor paths to this still-live process, not the compiler's /self/fd table.
    location = str(procfs.mount() / str(os.getpid()) / "fd" / str(directory))
    with open(src, "rb") as stream:
        source = stream.read()
    if hashlib.sha256(source).hexdigest() != tag:
        return "native source changed before compilation"
    tried = []
    for cc in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if not cc:
            continue
        temporary = receipt_name = source_name = None
        try:
            source_fd, source_name = tempfile.mkstemp(prefix=".source-", suffix=".c", dir=location)
            with os.fdopen(source_fd, "wb") as stream:
                stream.write(source)
            fd, temporary = tempfile.mkstemp(prefix=".build-", suffix=".so.tmp", dir=location)
            os.close(fd)
            result = subprocess.run(shlex.split(cc) + ["-O3", "-shared", "-fPIC", source_name,
                                                       "-o", temporary, "-lm"],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                tried.append(f"{cc}: exit {result.returncode}")
                continue
            temporary = os.path.basename(temporary)
            built = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                info = os.fstat(built)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise _UnsafeCache("compiler output is not a private regular file")
                os.fchmod(built, 0o600)
                digest = _digest(built)
                os.fsync(built)
            finally:
                os.close(built)
            receipt, receipt_name = tempfile.mkstemp(prefix=".build-", suffix=".json.tmp", dir=location)
            with os.fdopen(receipt, "w") as stream:
                json.dump({"version": 1, "source_sha256": tag, "library_sha256": digest}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "core.so", src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.replace(os.path.basename(receipt_name), "core.json", src_dir_fd=directory, dst_dir_fd=directory)
            receipt_name = None
            os.fsync(directory)
            return ""
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            tried.append(f"{cc}: {error}")
        finally:
            for name in (temporary, receipt_name, source_name):
                if name is not None:
                    try:
                        os.unlink(os.path.basename(name), dir_fd=directory)
                    except FileNotFoundError:
                        pass
    return "no working C compiler (" + "; ".join(tried) + ")" if tried else "no C compiler configured"


def _use_directory(src, directory, tag):
    lock = _file(directory, "build.lock", flags=os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            library = _load_existing(directory, tag)
            if library is not None:
                return library, ""
        except _UnsafeCache:
            raise
        except OSError:
            pass                              # a damaged trusted entry is rebuilt once
        reason = _compile(src, directory, tag)
        if reason:
            return None, reason
        try:
            library = _load_existing(directory, tag)
            return (library, "") if library is not None else (None, "compiled library was not verified")
        except _UnsafeCache:
            raise
        except OSError as error:
            return None, f"compiled library failed to load ({error})"
    finally:
        os.close(lock)


def _build():
    """Returns (library or None, reason)."""
    if os.environ.get("MESHPAINT_NO_NATIVE") == "1":
        return None, "MESHPAINT_NO_NATIVE=1"
    src = os.path.join(_HERE, "core.c")
    try:
        with open(src, "rb") as fh:
            tag = hashlib.sha256(fh.read()).hexdigest()
    except OSError as error:
        return None, f"cannot read bundled native source ({error})"
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    reasons = []
    directory = None
    try:
        directory = _primary_directory(cache, tag)
        library, reason = _use_directory(src, directory, tag)
        if library is not None:
            return library, ""
        reasons.append(reason)
    except (OSError, RuntimeError) as error:
        reasons.append(f"primary native cache unavailable ({error})")
    finally:
        if directory is not None:
            os.close(directory)
    # Never reuse a predictable directory in shared temporary storage.
    try:
        with tempfile.TemporaryDirectory(prefix="meshpaint-native-") as temporary:
            directory = _directory(temporary, private=True, create=False)
            try:
                library, reason = _use_directory(src, directory, tag)
                if library is not None:
                    return library, ""
                reasons.append(reason)
            finally:
                os.close(directory)
    except (OSError, RuntimeError) as error:
        reasons.append(f"private temporary native cache unavailable ({error})")
    return None, "; ".join(dict.fromkeys(reasons)) or "unknown"


def _load():
    global _lib, NATIVE, REASON
    if _lib is None:
        lib, REASON = _build()
        _lib = lib or False
        NATIVE = bool(_lib)
        if not NATIVE:
            print(f"meshpaint: compiled core not in use ({REASON}); using the Python core, which is much slower",
                  file=sys.stderr)
    return _lib


def _p(a, t):
    return a.ctypes.data_as(ctypes.POINTER(t))


def raster(xs, ys, q, faces, fmask, W, H, init=0.0, persp=True):
    """Returns (triangle id, bary1, bary2, q) buffers of shape (H, W)."""
    xs = np.ascontiguousarray(xs, np.float64); ys = np.ascontiguousarray(ys, np.float64)
    q = np.ascontiguousarray(q, np.float64)
    faces = np.ascontiguousarray(faces, np.int32); fmask = np.ascontiguousarray(fmask, np.uint8)
    Q = np.full((H, W), init, np.float32); TID = np.full((H, W), -1, np.int32)
    B1 = np.zeros((H, W), np.float32); B2 = np.zeros((H, W), np.float32)
    lib = _load()
    if lib:
        lib.mp_raster(_p(xs, ctypes.c_double), _p(ys, ctypes.c_double), _p(q, ctypes.c_double),
                      _p(faces, ctypes.c_int32), _p(fmask, ctypes.c_uint8), ctypes.c_int(len(faces)),
                      ctypes.c_int(W), ctypes.c_int(H), ctypes.c_int(int(persp)),
                      _p(Q, ctypes.c_float), _p(TID, ctypes.c_int32), _p(B1, ctypes.c_float), _p(B2, ctypes.c_float))
        return TID, B1, B2, Q
    return raster_py(xs, ys, q, faces, fmask, W, H, init, persp, (TID, B1, B2, Q))


def raster_py(xs, ys, q, faces, fmask, W, H, init=0.0, persp=True, buffers=None):
    """Pure-Python rasteriser; the reference for the compiled one."""
    if buffers is None:
        buffers = (np.full((H, W), -1, np.int32), np.zeros((H, W), np.float32), np.zeros((H, W), np.float32),
                   np.full((H, W), init, np.float32))
    TID, B1, B2, Q = buffers
    xs = np.asarray(xs, np.float64); ys = np.asarray(ys, np.float64); q = np.asarray(q, np.float64)
    faces = np.asarray(faces, np.int32); fmask = np.asarray(fmask, np.uint8)
    PX = np.arange(W) + 0.5; PY = (np.arange(H) + 0.5)[:, None]
    for f in np.nonzero(fmask)[0].tolist():
        i0, i1, i2 = faces[f]
        xa, xb, xc = xs[i0], xs[i1], xs[i2]; ya, yb, yc = ys[i0], ys[i1], ys[i2]
        qa, qb, qc = q[i0], q[i1], q[i2]
        if max(xa, xb, xc) < 0 or min(xa, xb, xc) >= W or max(ya, yb, yc) < 0 or min(ya, yb, yc) >= H:
            continue
        ar = (xb - xa) * (yc - ya) - (xc - xa) * (yb - ya)
        if abs(ar) < 1e-7:
            continue
        xm = max(int(min(xa, xb, xc)), 0); xM = min(int(max(xa, xb, xc)) + 1, W)
        ym = max(int(min(ya, yb, yc)), 0); yM = min(int(max(ya, yb, yc)) + 1, H)
        px = PX[xm:xM]; py = PY[ym:yM]
        w1 = ((px - xa) * (yc - ya) - (xc - xa) * (py - ya)) / ar
        w2 = ((xb - xa) * (py - ya) - (px - xa) * (yb - ya)) / ar
        w0 = 1 - w1 - w2
        iz = w0 * qa + w1 * qb + w2 * qc
        zb = Q[ym:yM, xm:xM]
        m = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4) & (iz > zb)
        if m.any():
            zb[m] = iz[m]; TID[ym:yM, xm:xM][m] = f
            with np.errstate(all="ignore"):
                B1[ym:yM, xm:xM][m] = (w1 * qb / iz)[m] if persp else w1[m]
                B2[ym:yM, xm:xM][m] = (w2 * qc / iz)[m] if persp else w2[m]
    return TID, B1, B2, Q


def strokes(canvas, height, P, reg, masks, BR, seed):
    """Lay strokes onto canvas in order. P is (n, 11): cx cy angle length width r g b opacity bend bristle."""
    H, W = height.shape
    P = np.ascontiguousarray(P, np.float32); reg = np.ascontiguousarray(reg, np.int32)
    lib = _load()
    if lib:
        lib.mp_strokes(_p(canvas, ctypes.c_float), _p(height, ctypes.c_float), ctypes.c_int(W), ctypes.c_int(H),
                       ctypes.c_int(len(P)), _p(P, ctypes.c_float), _p(reg, ctypes.c_int32),
                       _p(masks, ctypes.c_float), _p(BR, ctypes.c_float), ctypes.c_uint32(int(seed) & 0xFFFFFFFF))
        return
    strokes_py(canvas, height, P, reg, masks, BR, seed)


def strokes_py(canvas, height, P, reg, masks, BR, seed):
    """Pure-Python stroke painter. Mirrors core.c step for step in float32, including its
    xorshift32 bristle offsets, so both cores lay the same strokes."""
    H, W = height.shape
    P = np.asarray(P, np.float32); f32 = np.float32
    s = (int(seed) & 0xFFFFFFFF) or 1
    XS = np.arange(W, dtype=np.float32); YS = np.arange(H, dtype=np.float32)[:, None]
    for i in range(len(P)):
        cx, cy, th, L, w, cr, cg, cb, op, bend, bristle = P[i]
        s ^= (s << 13) & 0xFFFFFFFF; s ^= s >> 17; s ^= (s << 5) & 0xFFFFFFFF
        off = f32(s & 8191)
        r = f32(0.5) * L + f32(0.5) * w + abs(bend) + f32(2.0)
        x0 = max(int(cx - r), 0); x1 = min(int(cx + r) + 1, W)
        y0 = max(int(cy - r), 0); y1 = min(int(cy + r) + 1, H)
        if x1 <= x0 or y1 <= y0:
            continue
        dx = XS[x0:x1] - cx; dy = YS[y0:y1] - cy
        c, sn = np.cos(th), np.sin(th)                       # float32 in, float32 out
        u = dx * c + dy * sn; v = dy * c - dx * sn
        sf = max(f32(0.1) * w, f32(0.7)); nb = max(w * f32(0.9), f32(3.0))
        al = np.clip((f32(0.5) * L - np.abs(u)) / sf + f32(0.5), 0, 1)
        tc = np.clip(u / L + f32(0.5), 0, 1); arc = f32(4.0) * tc * (f32(1.0) - tc)
        vv = v - bend * (arc - f32(0.5))
        hw = f32(0.5) * w * (f32(0.5) + f32(0.5) * np.sqrt(arc))
        a = np.clip((hw - np.abs(vv)) / sf + f32(0.5), 0, 1) * al
        st = BR[(off + (vv / w + f32(0.5)) * nb).astype(np.int32) & 8191]
        a = a * np.clip(f32(1.0) - bristle * st * (f32(0.5) + f32(1.3) * tc), 0, 1) * op * masks[reg[i], y0:y1, x0:x1]
        a = a.astype(np.float32)
        patch = canvas[y0:y1, x0:x1]
        patch += (np.array((cr, cg, cb), np.float32) - patch) * a[..., None]
        hp = height[y0:y1, x0:x1]
        hp += ((f32(1.0) - f32(0.7) * st) - hp) * a
