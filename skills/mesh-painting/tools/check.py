"""Run public checks with this Python interpreter; use --slow for the complete suite."""
import argparse
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
RUN_TESTS = """import sys
import unittest

loader = unittest.defaultTestLoader
suite = loader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv) > 1 else loader.discover('tests', pattern='test_*.py')
if suite.countTestCases() == 0:
    print('No tests were selected; refusing an empty verification run.', file=sys.stderr)
    raise SystemExit(5)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slow", action="store_true", help="enable full-size and process-control slow checks")
    parser.add_argument("--test", action="append", metavar="NAME",
                        help="run a unittest module, class or method; repeat to select several")
    args = parser.parse_args(argv)
    missing = [name for name in ("numpy", "scipy", "PIL") if importlib.util.find_spec(name) is None]
    if missing:
        print("Missing Python dependencies: " + ", ".join(missing), file=sys.stderr)
        command = [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
        print("Install with: " + shlex.join(command), file=sys.stderr)
        return 2
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    slow = args.slow or env.get("MESHPAINT_SLOW") == "1"
    if slow:
        env["MESHPAINT_SLOW"] = "1"
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("OMP_NUM_THREADS", "1")
    print(f"Python: {sys.executable} ({sys.version.split()[0]})", flush=True)
    print("Slow checks: " + ("enabled" if slow else "disabled"), flush=True)
    command = [sys.executable, "-B", "-c", RUN_TESTS]
    if args.test:
        command += args.test
        cwd = ROOT / "tests"
    else:
        cwd = ROOT
    try:
        return subprocess.call(command, cwd=cwd, env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
