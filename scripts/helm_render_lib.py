"""Compatibility alias: the implementation moved to sirosid_core/helm.py.

`import helm_render_lib` from a script yields the package module itself, so every existing
caller (and its attribute access) keeps working unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core import helm as _module  # noqa: E402

sys.modules[__name__] = _module
