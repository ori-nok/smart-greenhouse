#!/usr/bin/env python3
"""Копирует общие файлы прошивки (firmware/shared) в каждый проект Wokwi.
Проект Wokwi самодостаточен: sketch.ino + gh_logic.h + gh_common.h + diagram.json + libraries.txt.
Режим --check завершается с ошибкой, если копии разошлись (используется в CI)."""
import os, shutil, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware")
NODES = ["gh-soil-01", "gh-tank-01", "gh-climate-01"]
SHARED = ["gh_logic.h", "gh_common.h"]
bad = False
for n in NODES:
    for f in SHARED:
        src, dst = os.path.join(ROOT, "shared", f), os.path.join(ROOT, n, f)
        if "--check" in sys.argv:
            if not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read():
                print("MISMATCH:", dst); bad = True
        else:
            shutil.copyfile(src, dst)
if "--check" in sys.argv:
    sys.exit(1 if bad else 0)
print("synced")
