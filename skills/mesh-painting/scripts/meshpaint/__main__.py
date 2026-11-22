"""Command line:  python3 scripts/run.py SCENE.py --out NAME --stage preview|render|paint

Use the launcher (scripts/run.py): it puts the engine on the import path and stops Python
writing bytecode into the skill folder.

preview  small, fast render with lighting and lens. Check composition and light here.
render   full-size render, no painting, plus zoom tiles. Check detail here before the long step.
paint    full-size render, then the brushstroke painting, plus zoom tiles.

Every run works inside a resource level (--level draft|standard|high; standard unless you say
otherwise). A run that would go over its level stops with exit code 3, says which stage hit which
limit, and never replaces an image with an unfinished one. It never switches level or changes the
scene by itself.
"""
import argparse
import fcntl
import glob
import json
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time
import traceback

from . import limits, procfs
from .looks import LOOKS

KEPT_SUFFIXES = (".png", "_full.png", "_partial.png", "_render.png", "_preview.png")
_STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}      # held across each fork so none is lost or mishandled in between


def _display_path(path):
    """Present a caller's path relative to the launch directory; keep the path used for I/O intact."""
    if not os.path.isabs(path):
        return path
    cwd = Path.cwd()
    common = os.path.commonpath((cwd, path))
    # Keep parent components in the user's path: they can follow a symlink.
    return str(Path(os.path.relpath(common, cwd)) / Path(path).relative_to(common))


class Outputs:
    """Images are written under temporary names and take their real names only when the stage that
    made them has finished, so a stopped run never damages an image from an earlier one."""

    def __init__(self, tell=None):
        self.tag = f".part-{os.getpid()}"
        self.pending, self.done = [], []
        self.tell = tell or (lambda **msg: None)     # each rename is reported as it happens

    def save(self, image, path):
        image.save(path + self.tag, format="PNG")
        self.pending.append(path)
        return path

    def commit(self):
        """Give the pending images their real names. A stop or time limit that arrives during the
        renames is held until they are all done, so a stage's images change together."""
        limits.hold()
        try:
            while self.pending:
                path = self.pending[0]
                os.replace(path + self.tag, path)
                self.done.append(self.pending.pop(0))
                self.tell(image=path)
        finally:
            limits.release()

    def discard(self):
        for path in self.pending:
            try:
                os.remove(path + self.tag)
            except OSError:
                pass
        self.pending = []


def _to_image(arr):
    import numpy as np
    from PIL import Image
    if not np.isfinite(arr).all():
        raise ValueError("meshpaint: image contains non-finite pixel values (NaN or infinity); "
                         "refusing to write an invalid image")
    return Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8))


def tiles(arr, prefix, grid, outs):
    cols, rows = (int(v) for v in grid.lower().split("x"))
    H, W = arr.shape[:2]
    out = []
    for r in range(rows):
        for c in range(cols):
            p = f"{prefix}_tile_r{r + 1}c{c + 1}.png"
            outs.save(_to_image(arr[r * H // rows:(r + 1) * H // rows, c * W // cols:(c + 1) * W // cols]), p)
            out.append(p)
    return out


def _start_time(pid):
    """When `pid` started, in clock ticks since boot (Linux procfs), or None."""
    try:
        with open(procfs.mount() / str(pid) / "stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return None


def _is_meshpaint(pid, started=None):
    """True if `pid` is the live run that wrote the pid file, so --stop never signals a stranger: the
    process must exist, not be a zombie, and have the start time the pid file recorded (a pid can be
    reused; a pid file with no start time proves nothing)."""
    try:
        with open(procfs.mount() / str(pid) / "stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return fields[0] != b"Z" and fields[19].decode() == started
    except (OSError, IndexError):
        return False


def _read_pidfile(pidfile):
    """(pid, start time or None) from a pid file; raises OSError or ValueError if it cannot be read."""
    with open(pidfile) as fh:
        parts = fh.read().split()
    if not parts:
        raise ValueError("the pid file is empty")
    return int(parts[0]), (parts[1] if len(parts) > 1 else None)


def _lock_output(prefix):
    """Claim a persistent coordination inode before inspecting or changing its PID file.

    Forked supervision processes inherit this open description. Close our
    descriptor to release our reference; explicitly unlocking would also
    unlock the references still protecting a surviving supervisor/renderer.
    The coordination file is never removed, so contenders lock the same inode.
    """
    fd = os.open(prefix + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise OSError("the output coordination file must be a regular file with one link")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _refuse_output(prefix, pidfile):
    try:
        other, _ = _read_pidfile(pidfile)
    except (OSError, ValueError):
        other = None
    owner = f" ({other})" if other else ""
    print(f"meshpaint: another run{owner} is already writing to {_display_path(prefix)}; wait for it, stop it with --stop, "
          "or use another --out", file=sys.stderr)
    raise SystemExit(2)


def _remove_stopped_pidfile(pidfile, identity):
    """A stopping command must never remove a successor run's PID file."""
    try:
        fd = _lock_output(pidfile[:-4])
    except OSError:                                  # active owner, or unavailable output: leave its state alone
        return
    try:
        try:
            if _read_pidfile(pidfile) == identity:
                os.remove(pidfile)
        except (OSError, ValueError):
            pass
    finally:
        os.close(fd)


def stop(pidfile):
    try:
        pid, started = _read_pidfile(pidfile)
    except (OSError, ValueError) as e:
        print(f"nothing to stop: {e}")
        return
    if not _is_meshpaint(pid, started):
        print(f"process {pid} is not a meshpaint run; no signal sent.")
    else:
        os.kill(pid, signal.SIGTERM)
        for _ in range(300):                      # wait up to 30 s for it to exit
            if not _is_meshpaint(pid, started):
                break
            time.sleep(0.1)
        if _is_meshpaint(pid, started):           # the supervisor kills a renderer that has not stopped 20 s after being asked
            print(f"asked {pid} to stop; it has not exited yet. It removes its own pid file when it does.")
            return
        print(f"stopped {pid}")
    _remove_stopped_pidfile(pidfile, (pid, started))


def _job(a, outs):
    """The render itself. Runs in the renderer process, after the ceilings are in place."""
    limits.stage("loading the engine")
    import importlib.util
    import numpy as np
    from PIL import Image
    from . import pipeline, util
    from .paint import paint, planned_strokes
    from .scene import validate_regions

    util.seed(a.seed)
    limits.stage("scene build")
    spec = importlib.util.spec_from_file_location("scene_module", a.scene)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    sc = mod.build()
    limits.reraise_latched()                         # a limit hit in a scene thread, or caught by the scene
    validate_regions(sc)                            # reject unsupported IDs before allocating any rendering buffers
    S = 1.0 if a.stage == "preview" else a.scale
    limits.stage("job check")
    if int(sc.size[0] * S) < 16 or int(sc.size[1] * S) < 16:
        raise SystemExit(f"meshpaint: the canvas would be {int(sc.size[0] * S)} x {int(sc.size[1] * S)} pixels; "
                         "check --scale and Scene.size")
    limits.check_pixels("job check (canvas size)", int(sc.size[0] * S), int(sc.size[1] * S))
    if a.stage == "paint":                           # refuse the painting before the render is paid for
        limits.check_strokes("job check (planning the passes)", planned_strokes(S * sc.size[0] / 1600.0))
    log = pipeline.make_log()
    look = dict(sun_offset=a.sun[0], sun_elev=a.sun[1]) if a.sun else a.look
    r = pipeline.render(sc, S, log, look)
    if a.stage == "preview":
        limits.stage("writing images")
        outs.save(_to_image(r["target"]), a.out + "_preview.png")
        limits.stop_timer(); outs.commit()
        print("wrote", _display_path(a.out) + "_preview.png")
        return
    small = lambda arr: _to_image(arr).resize(sc.size, Image.LANCZOS)
    if a.stage == "render":
        limits.stage("writing images")
        written = [outs.save(small(r["target"]), a.out + "_render.png")]
        written += tiles(r["target"], a.out + "_render", a.tiles, outs) if a.tiles != "0" else []
    else:
        outs.save(small(r["target"]), a.out + "_render.png"); outs.commit()     # survives if the painting is stopped
        log("render kept as " + _display_path(a.out) + "_render.png")
        last = {}

        def checkpoint(canvas, passes, strokes):     # the canvas as it stood after the last whole pass
            if "canvas" not in last:
                last["canvas"] = np.empty_like(canvas)
            np.copyto(last["canvas"], canvas); last["passes"] = passes
        try:
            canvas, n = paint(r["target"], r["regions"], sc.stroke_angle, r["S"], r["horizon_y"], util.rng(), log,
                              checkpoint=checkpoint)
            log(f"painted with {n} strokes")
            limits.stage("writing images")
            outs.save(_to_image(canvas), a.out + "_full.png")
            written = [outs.save(small(canvas), a.out + ".png"), a.out + "_full.png"]
            written += tiles(canvas, a.out, a.tiles, outs) if a.tiles != "0" else []
        except (limits.LimitExceeded, MemoryError):
            limits.stop_timer()
            try:                                     # keep the painting as it stood after the last whole pass
                outs.discard()
                if "canvas" in last:
                    outs.save(small(last["canvas"]), a.out + "_partial.png"); outs.commit()
                    log(f"painting as it stood after pass {last['passes']} kept as {_display_path(a.out)}_partial.png")
            except BaseException:                    # best effort (memory may not allow it); the limit stop stands
                outs.discard()
            raise
        try:
            os.remove(a.out + "_partial.png")        # left by an earlier stopped run; now out of date
        except OSError:
            pass
    limits.stop_timer(); outs.commit()
    print("wrote", *(_display_path(p) for p in written), sep="\n  ")


def _renderer(a, budget, wfd, parent):
    """Body of the renderer process. Returns its exit code."""
    def tell(**msg):
        try:
            os.write(wfd, (json.dumps(msg) + "\n").encode())
        except OSError:
            pass

    def _terminate(signum, frame):                   # --stop, or the supervisor going away
        budget.interrupted = True                    # remembered, in case the scene file swallows it
        limits.raise_or_hold(SystemExit(143))
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, signal.SIG_IGN)     # Ctrl-C is the supervisor's to pass on, as a request to stop
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)    # held by the supervisor across the fork
    try:
        os.setpgid(0, 0)                             # own process group, so what the scene starts can be killed with it
    except OSError:
        pass
    try:                                             # Linux: be told if the supervisor dies
        import ctypes
        ctypes.CDLL(None).prctl(1, int(signal.SIGTERM), 0, 0, 0)
    except Exception:
        pass
    outs = Outputs(tell)
    code, stop = 0, None
    try:
        try:
            if os.getppid() != parent:
                raise SystemExit(143)
            budget.on_stage = lambda name: tell(stage=name)
            limits.apply_memory_ceiling()            # both ceilings are in place before the scene file loads
            limits.start_timer()
            limits.lock()                            # the level cannot be changed from here on
            tell(memory_enforced=True, memory_mb=budget.level.memory_mb)
            _job(a, outs)
            limits.reraise_latched()
        except limits.LimitExceeded as e:
            stop = e.as_dict()
        except MemoryError as e:
            stop = limits.memory_exceeded(e).as_dict()
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if code == limits.EXIT_LIMIT:            # that code is reserved for a limit stop
                code = 1
        except KeyboardInterrupt:
            code = 130
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            limits.stop_timer()
            outs.discard()
    except limits.LimitExceeded as e:                # the time limit fired again while the first stop was being handled
        limits.stop_timer()
        outs.discard()
        stop = stop or e.as_dict()
    if stop:
        code = limits.EXIT_LIMIT
    tell(stop=stop, usage=budget.usage(), memory_mb=budget.level.memory_mb)
    if os.getppid() != parent:                       # the supervisor is gone: nobody else will stop what the scene started
        try:
            sys.stdout.flush(); sys.stderr.flush()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(os.getpgrp(), signal.SIGKILL) if os.getpgrp() == os.getpid() else None
        except OSError:
            pass
    return code


def _children_of(pid):
    """Pids whose parent is `pid` (Linux procfs; empty elsewhere)."""
    out = set()
    try:
        root = procfs.mount()
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            try:
                with open(root / name / "stat", "rb") as fh:
                    fields = fh.read().rsplit(b")", 1)[1].split()
                if int(fields[1]) == pid:
                    out.add(int(name))
            except (OSError, ValueError, IndexError):
                pass
    return out


def _kill_leftovers():
    """Kill and reap what the renderer left running. This runs in the supervisor, a process that exists
    only for this run and is the 'subreaper' of its descendants: on Linux a process the renderer started
    becomes the supervisor's child when its own parent goes, whatever session or group it moved to. So
    every child the supervisor has at this point belongs to the run. Returns the pids still alive after 5 s."""
    me = os.getpid()
    give_up = time.monotonic() + 5.0
    kids = set()
    while time.monotonic() < give_up:
        kids = _children_of(me)
        if not kids:
            return []
        for k in kids:
            try:
                os.kill(k, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.02)
        for k in kids:
            try:
                os.waitpid(k, os.WNOHANG)
            except OSError:
                pass
    return sorted(kids)


class _Watch:
    """What the supervisor knows about one renderer. Kept outside the loop so an interrupted wait can resume."""

    def __init__(self, pid, rfd, budget):
        self.pid, self.rfd, self.budget = pid, rfd, budget
        self.left_running = []
        self.reaped = False
        self.seen = {"stage": "process setup", "images": []}
        self.buf = b""
        self.kill_at = budget.t0 + budget.level.seconds + limits.KILL_GRACE
        self.kill_reason = "time"                    # or "stop": asked to stop and did not
        self.killed = None
        self.status = self.rusage = None
        os.set_blocking(rfd, False)

    def insist(self, seconds, reason="stop"):
        """The renderer has been asked to stop; kill it if it has not within `seconds`."""
        at = time.monotonic() + seconds
        if at < self.kill_at:
            self.kill_at, self.kill_reason = at, reason

    def drain(self):
        while True:
            try:
                data = os.read(self.rfd, 65536)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            self.buf += data
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "image" in msg:
                    self.seen["images"].append(msg.pop("image"))
                self.seen.update(msg)

    def kill_group(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            pass

    def wait(self):
        """Wait for the renderer, collecting what it reports. Kills it, and anything it started, if it
        outlives its time limit (or a request to stop) by more than the grace period."""
        while self.status is None:
            try:                                     # has it exited? (look without reaping, so its pid and group stay ours)
                gone = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                gone = True
            if gone:
                self.kill_group()                    # the renderer has gone; so must anything it left in its group
                try:
                    reaped = os.wait4(self.pid, 0)
                except ChildProcessError:
                    reaped = (0, 1 << 8, None)
                self.reaped = True
                self.left_running = _kill_leftovers()            # ... and anything that had moved out of the group
                self.drain()
                self.status, self.rusage = reaped[1], reaped[2]
                break
            left = self.kill_at - time.monotonic()
            if left <= 0 and not self.killed:
                self.killed = self.kill_reason
                self.kill_group()
                try:
                    os.kill(self.pid, signal.SIGKILL)
                except OSError:
                    pass
            select.select([self.rfd], [], [], 0.05 if self.killed else min(max(left, 0.05), 0.2))
            self.drain()


def _run(a, budget, state, release_to):
    """Run the job under a supervisor process and return its exit code. The calling process (the
    launcher, or a program that called main()) only starts the supervisor, passes on a request to stop,
    and waits; its own children and process settings are left alone. main() has already put the
    non-raising stop handlers in place and is holding SIGTERM and SIGINT; they are let through
    (`release_to` is the caller's original mask) once there is a supervisor to pass them to."""
    if not hasattr(os, "fork"):
        print("meshpaint: this system cannot run the renderer as a limited process, so resource limits "
              "cannot be enforced; refusing to run without them", file=sys.stderr)
        return limits.EXIT_LIMIT
    sys.stdout.flush(); sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            code = _supervisor(a, budget)            # installs its own handlers, then lets the held signals through
        except BaseException:
            traceback.print_exc()
        finally:
            try:
                sys.stdout.flush(); sys.stderr.flush()
            finally:
                os._exit(code)
    state["pid"] = pid
    try:
        if release_to is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, release_to)     # anything held is now delivered and passed on
        # Bound each wait so pending Python stop handlers can run promptly.
        while True:
            reaped, status = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                break
            time.sleep(0.05)
    finally:
        state["pid"] = None
    if os.WIFSIGNALED(status):
        print(f"meshpaint: the supervisor process died from signal {os.WTERMSIG(status)}; the renderer was told to stop",
              file=sys.stderr)
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _supervisor(a, budget):
    """Body of the supervisor process: start the renderer with its ceilings, wait for it, clean up, report.
    Returns the exit code. Holds no scene data and never loads numpy or the scene."""
    try:                                             # Linux: orphans of the renderer become this process's children
        import ctypes
        libc = ctypes.CDLL(None)
        libc.prctl(36, 1, 0, 0, 0)
        libc.prctl(1, int(signal.SIGTERM), 0, 0, 0)  # and be told to stop if the calling process dies
    except Exception:
        pass
    rfd, wfd = os.pipe()
    sys.stdout.flush(); sys.stderr.flush()
    parent = os.getpid()
    child = {"pid": None, "asked": None, "watch": None}

    def _forward(signum, frame):                     # a request to stop: note it, pass it on, never raise
        if child["asked"] is None:
            child["asked"] = "interrupt" if signum == signal.SIGINT else "stop"
        if child["pid"] and not (child["watch"] and child["watch"].reaped):
            try:
                os.kill(child["pid"], signal.SIGTERM)
            except OSError:
                pass
        if child["watch"]:                           # ask once, then insist
            child["watch"].insist(10.0 if child["asked"] == "interrupt" else limits.KILL_GRACE)
    signal.signal(signal.SIGTERM, _forward)          # in place before the renderer exists
    if signal.getsignal(signal.SIGINT) != signal.SIG_IGN:         # (a caller that ignores Ctrl-C keeps ignoring it)
        signal.signal(signal.SIGINT, _forward)       # Ctrl-C may arrive twice (terminal and launcher); both are harmless
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)    # held by the launcher across its fork
    signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)      # and held again across this one
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(rfd)
            code = _renderer(a, budget, wfd, parent)
        finally:
            try:
                sys.stdout.flush(); sys.stderr.flush()
            finally:
                os._exit(code)
    os.close(wfd)
    watch = _Watch(pid, rfd, budget)
    child["pid"], child["watch"] = pid, watch
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)
    if child["asked"]:                               # a stop arrived before the renderer existed
        _forward(signal.SIGINT if child["asked"] == "interrupt" else signal.SIGTERM, None)
    try:
        watch.wait()
    finally:
        os.close(rfd)
        for p in glob.glob(glob.escape(a.out) + f"*.part-{pid}"):     # anything a killed renderer left half-written
            try:
                os.remove(p)
            except OSError:
                pass

    status, seen, ru = watch.status, watch.seen, watch.rusage
    interrupted = child["asked"] == "interrupt"
    stop = seen.get("stop")
    usage = dict(seen.get("usage") or {}, last_stage=seen.get("stage"))
    usage["seconds"] = round(budget.elapsed(), 1)
    if ru is not None:
        usage["peak_rss_kb_from_os"] = int(ru.ru_maxrss)
    allowed = budget.allowed()
    if seen.get("memory_mb"):
        allowed["memory_mb"] = seen["memory_mb"]    # lower than the level's if something outside had set a tighter one
    failure = None
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        if watch.killed == "time":
            stop = dict(stage=seen.get("stage"), limit="time", unit="s", requested=None, used=usage["seconds"],
                        allowed=budget.level.seconds, level=budget.level.name,
                        detail=f"the renderer did not stop by itself within {limits.KILL_GRACE:.0f} s of the limit and was killed")
            code = limits.EXIT_LIMIT
        elif watch.killed == "stop":
            code = 130 if interrupted else 143       # asked to stop, did not, and was killed
        elif child["asked"] and sig in (signal.SIGTERM, signal.SIGKILL):
            code = 130 if interrupted else 143       # stopped on request before it could set up its own handling
        else:
            failure = f"the renderer process died from signal {sig} during stage '{seen.get('stage')}'"
            code = 128 + sig
    else:
        code = os.WEXITSTATUS(status)
        if code == limits.EXIT_LIMIT:
            if not stop:                             # e.g. a scene file calling os._exit(3): not a limit stop
                failure = "the renderer exited with the code reserved for a limit stop but sent no report"
                code = 1
        elif code == 0 and (seen.get("usage") or {}).get("timed_out"):
            # only possible if the scene file swallowed the time limit; the images were written, but say so
            stop = dict(stage=seen.get("stage"), limit="time", unit="s", requested=None, used=seen["usage"]["seconds"],
                        allowed=budget.level.seconds, level=budget.level.name,
                        detail="the run ignored its time limit and finished late; its images were written")
            code = limits.EXIT_LIMIT
        elif code not in (0, 143, 130):
            failure = f"the renderer exited with code {code} during stage '{seen.get('stage')}'"
    if child["asked"] and not stop and code != 0:    # a request to stop, however the renderer went down
        code, failure = (130 if interrupted else 143), None
    kept = [(a.out + sfx) for sfx in KEPT_SUFFIXES if os.path.exists(a.out + sfx)]
    made = set(seen["images"])
    result = "stopped" if stop else "completed" if code == 0 else "interrupted" if code in (143, 130) else "failed"
    report = dict(level=budget.level.name, stage=a.stage, scale=(1.0 if a.stage == "preview" else a.scale),
                  scene=_display_path(a.scene), result=result, stop=stop, failure=failure, allowed=allowed, usage=usage,
                  memory_ceiling_enforced=bool(seen.get("memory_enforced")),
                  images_kept=[dict(path=_display_path(p), written_by_this_run=p in made) for p in kept],
                  images_written_by_this_run=sorted(_display_path(p) for p in made), processes_left_running=watch.left_running)
    try:
        part = f"{a.out}_usage.json.part-{os.getpid()}"
        with open(part, "w") as fh:
            json.dump(report, fh, indent=1)
        os.replace(part, a.out + "_usage.json")
    except OSError as e:
        print(f"meshpaint: could not write the usage report: {e}", file=sys.stderr)
    kept_line = ("meshpaint: images kept: " + ", ".join(_display_path(p) + (" (from this run)" if p in made else " (from an earlier run)") for p in kept)
                 if kept else "meshpaint: no image exists yet for this output name")
    if stop:
        print("meshpaint: stopped by a resource limit. " + limits.describe(stop), file=sys.stderr)
        print(kept_line, file=sys.stderr)
        print(f"meshpaint: the scene file and the level '{budget.level.name}' were left as they are; nothing was retried. "
              f"Full figures: {_display_path(a.out)}_usage.json", file=sys.stderr)
    elif failure and (os.WIFSIGNALED(status) or "no report" in failure or code != 1):
        print(f"meshpaint: {failure}.", file=sys.stderr)
        print(kept_line, file=sys.stderr)
        print(f"meshpaint: figures: {_display_path(a.out)}_usage.json", file=sys.stderr)
    if watch.left_running:
        print(f"meshpaint: processes started by the scene file could not be stopped and are still running: "
              f"{', '.join(map(str, watch.left_running))}", file=sys.stderr)
    if code == 0:
        u, lv = usage, budget.level
        print(f"level {lv.name}: {u.get('vertices', 0):,} of {lv.vertices:,} vertices, {u.get('triangles', 0):,} of {lv.triangles:,} triangles, "
              f"{u.get('pixels', 0):,} of {lv.pixels:,} pixels, {u.get('strokes_done', 0):,} of {lv.strokes:,} strokes, "
              f"peak memory {u.get('peak_address_space_kb', 0) // 1024:,} of {allowed['memory_mb']:,} MiB, {u['seconds']:.0f} of {lv.seconds:,} s")
    return code


def main(own_process=False):
    """`own_process`: this process exists only to be the launcher (scripts/run.py, python -m meshpaint),
    so its signal handling need not be handed back at the end."""
    ap = argparse.ArgumentParser(prog="meshpaint", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scene", nargs="?", help="scene file defining build() -> meshpaint.scene.Scene")
    ap.add_argument("--out", default="out/painting", help="output path prefix")
    ap.add_argument("--stage", choices=("preview", "render", "paint"), default="preview")
    ap.add_argument("--level", choices=tuple(limits.LEVELS), default=limits.DEFAULT_LEVEL,
                    help="resource level: hard caps on vertices, triangles, pixels, strokes, memory and time (default standard)")
    ap.add_argument("--scale", type=float, default=None,
                    help="supersampling for render/paint; default is the level's own: "
                         + ", ".join(f"{k} {v.scale}" for k, v in limits.LEVELS.items()) + " (2.4 = 3840 wide)")
    ap.add_argument("--look", help="override the scene's look: " + ", ".join(LOOKS))
    ap.add_argument("--sun", nargs=2, type=float, metavar=("OFFSET", "ELEV"), help="custom sun: degrees from view direction, elevation")
    ap.add_argument("--tiles", default="3x2", help="zoom tiles to write for review, COLSxROWS (0 to skip)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--time-limit", type=float, metavar="SECONDS", help="a shorter time limit than the level's (can only lower it)")
    ap.add_argument("--memory-limit", type=int, metavar="MIB", help="a lower memory ceiling than the level's (can only lower it)")
    ap.add_argument("--stop", action="store_true", help="stop the run that owns --out (uses its pid file)")
    a = ap.parse_args()
    pidfile = a.out + ".pid"
    if not (a.stop or a.scene):
        ap.error("scene file required")
    try:
        procfs.mount()  # Verify process identity and monitoring before reading or creating a pid file.
    except RuntimeError as error:
        ap.exit(1, f"meshpaint: {error}\n")
    if a.stop:
        stop(pidfile)
        return
    if a.look and a.look not in LOOKS:
        print(f"meshpaint: unknown look '{a.look}'; valid looks: {', '.join(LOOKS)}", file=sys.stderr)
        sys.exit(2)
    if a.scale is None:
        a.scale = limits.LEVELS[a.level].scale
    if not 0 < a.scale < float("inf"):
        ap.error("--scale must be a positive number")
    try:
        budget = limits.use(a.level, memory_mb=a.memory_limit, seconds=a.time_limit)
    except ValueError as e:
        ap.error(str(e))
    # From the moment the pid file exists a request to stop must be neither lost nor fatal to this
    # process. So, before the file is written: hold SIGTERM and SIGINT, and install handlers that only
    # pass the request on to the supervisor. Everything is put back before main() returns.
    state = {"pid": None, "handlers": None}
    ownership_fd = None

    def _pass_on(signum, frame):
        if state["pid"]:
            try:
                os.kill(state["pid"], signum)
            except OSError:
                pass
    orig_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    try:
        try:
            was_int = signal.getsignal(signal.SIGINT)
            state["handlers"] = (signal.signal(signal.SIGTERM, _pass_on),
                                 was_int if was_int == signal.SIG_IGN else signal.signal(signal.SIGINT, _pass_on))
        except ValueError:                           # not the main thread of a calling program: no handlers possible,
            state["handlers"] = None                 # so a signal is handled as that program handles it
        try:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            try:
                ownership_fd = _lock_output(a.out)
            except BlockingIOError:
                _refuse_output(a.out, pidfile)
            try:
                other, other_started = _read_pidfile(pidfile)
            except (OSError, ValueError):
                other = other_started = None
            if other and other != os.getpid() and _is_meshpaint(other, other_started):
                _refuse_output(a.out, pidfile)
            with open(pidfile, "w") as fh:
                fh.write(f"{os.getpid()} {_start_time(os.getpid()) or ''}".strip())
        except OSError as e:
            print(f"meshpaint: cannot write to the output location: {e}", file=sys.stderr)
            sys.exit(2)
        try:
            code = _run(a, budget, state, orig_mask)
        finally:
            try:
                os.remove(pidfile)
            except OSError:
                pass
    finally:
        if ownership_fd is not None:
            os.close(ownership_fd)
        if not own_process:                          # a calling program gets its own handling back, exactly
            signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
            if state["handlers"]:
                signal.signal(signal.SIGTERM, state["handlers"][0]); signal.signal(signal.SIGINT, state["handlers"][1])
            signal.pthread_sigmask(signal.SIG_SETMASK, orig_mask)
        else:
            # Run as its own process: with the run over, a late stop request must not turn the exit status
            # into a death by signal, and the interpreter puts default handling back while it shuts down.
            # So the two signals are ignored and held from here until the process has exited.
            signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
            if state["handlers"]:
                signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGINT, signal.SIG_IGN)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main(own_process=True)
