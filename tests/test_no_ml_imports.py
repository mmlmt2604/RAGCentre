"""The core must not import machine-learning or numeric libraries (section 2)."""

from __future__ import annotations

import subprocess
import sys

SCRIPT = r"""
import importlib, pkgutil, sys
import ragcentre
for mod in pkgutil.walk_packages(ragcentre.__path__, "ragcentre."):
    importlib.import_module(mod.name)
bad = sorted(m for m in ("numpy", "torch", "transformers", "tiktoken") if m in sys.modules)
print(",".join(bad))
"""


def test_no_ml_dependencies_imported() -> None:
    out = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "", f"forbidden modules imported: {out.stdout.strip()}"
