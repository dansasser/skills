"""Launcher: python3 scripts/run.py SCENE.py --out out/NAME --stage preview|render|paint   (see --help)"""
import os
import sys

sys.dont_write_bytecode = True      # never write into the skill directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from meshpaint.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main(own_process=True)
