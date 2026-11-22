"""Shared helpers for the engine tests. Run with:  <python> -B -m unittest discover -s tests

Slow checks (full-size paint timing, Python-core paint, stop handling) run only when
MESHPAINT_SLOW=1.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
RUN = os.path.join(SCRIPTS, "run.py")
LIGHTHOUSE = os.path.join(ROOT, "assets", "scenes", "lighthouse.py")
SLOW = os.environ.get("MESHPAINT_SLOW") == "1"
sys.dont_write_bytecode = True
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

GENERATED_DIRECTORIES = {".git", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache",
                         ".ruff_cache", "workspace", "out", "build", "dist"}


def _tracked_source_files(root):
    """Use this source's index only; a Gitless copy may sit inside another repository."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, env=env)
        if top.returncode or os.path.realpath(top.stdout.strip()) != os.path.realpath(root):
            return None
        result = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                                capture_output=True, text=True, env=env)
    except OSError:
        return None
    if result.returncode:
        return None
    # Keep missing entries visible: source completeness checks must fail rather than hide them.
    return sorted(path for path in result.stdout.split("\0") if path)


def public_source_files(root=ROOT):
    """The complete indexed source, or the complete source tree when copied without Git.

    No development-document or unknown-file allowlist is applied. Review the
    actual public tree; generated work is the only filesystem-copy exclusion.
    """
    root = os.fspath(root)
    tracked = _tracked_source_files(root)
    if tracked is not None:
        return tracked
    files = []
    for directory, subdirectories, names in os.walk(root):
        kept = []
        for name in subdirectories:
            if name in GENERATED_DIRECTORIES or name.startswith(".venv"):
                continue
            path = os.path.join(directory, name)
            if os.path.islink(path):
                files.append(os.path.relpath(path, root))
            else:
                kept.append(name)
        subdirectories[:] = kept
        files.extend(os.path.relpath(os.path.join(directory, name), root) for name in names
                     if not name.endswith((".pyc", ".pyo")))
    return sorted(files)


def copy_files(files, dest, root=ROOT):
    for f in files:
        source = os.path.join(root, f)
        if os.path.islink(source):
            raise ValueError(f"public source contains a link: {f}")
        os.makedirs(os.path.dirname(os.path.join(dest, f)) or dest, exist_ok=True)
        shutil.copy2(source, os.path.join(dest, f))


def tree_hashes(root):
    out = {}
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            out[os.path.relpath(p, root)] = sha(p)
    return out


def run_engine(args, cwd=None, env=None, timeout=900, runner=RUN):
    e = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    e.update(env or {})
    return subprocess.run([sys.executable, "-B", runner] + list(args), cwd=cwd or tempfile.gettempdir(),
                          env=e, capture_output=True, text=True, timeout=timeout)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
