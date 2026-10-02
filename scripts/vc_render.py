"""Compatibility alias: the implementation moved to sirosid_core/vc_render.py.

`import vc_render` from a script yields the package module itself, so every existing
caller (and its attribute access) keeps working unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core import vc_render as _module  # noqa: E402

sys.modules[__name__] = _module
