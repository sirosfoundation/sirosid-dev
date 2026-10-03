"""Compatibility alias: the implementation moved to sirosid_core/api_auth.py.

`import api_auth` from a script yields the package module itself, so every existing
caller (and its attribute access) keeps working unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core import api_auth as _module  # noqa: E402

sys.modules[__name__] = _module
