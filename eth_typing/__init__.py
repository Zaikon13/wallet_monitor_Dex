"""Compatibility shim for :mod:`eth_typing`.

The upstream ``eth-typing`` package recently removed a handful of type aliases
that older optional pytest plugins (such as ``web3.tools.pytest_ethereum``)
still import unconditionally.  The absence of these aliases causes the plugin
to crash during import which, in turn, prevents pytest from even starting.  The
project under test does not rely on that plugin directly, but we still need the
import to succeed so that the test discovery phase can continue.

To remain compatible with the rest of the ecosystem we delegate to the real
``eth_typing`` package that is installed in the environment and then add back
the missing alias when required.  This mirrors the behaviour of older releases
without pulling in any of the deprecated dependencies.
"""

from __future__ import annotations

from importlib.machinery import PathFinder
from importlib.util import module_from_spec
from typing import Any, Dict, NewType
import sys
from types import ModuleType

MODULE_NAME = __name__


def _load_upstream() -> ModuleType:
    """Load the upstream :mod:`eth_typing` module from ``site-packages``."""

    current_file = __file__
    for entry in sys.path[1:]:
        if not entry:
            continue
        spec = PathFinder.find_spec(MODULE_NAME, [entry])
        if spec is None or spec.origin == current_file or spec.loader is None:
            continue
        module = module_from_spec(spec)
        # Register the module before executing it so that any recursive imports
        # resolve to the same object.
        sys.modules[MODULE_NAME] = module
        spec.loader.exec_module(module)
        return module
    raise ImportError("Unable to locate the upstream eth_typing module")


_upstream_module = _load_upstream()

_FALLBACK_ALIASES = {
    "ContractName": lambda: NewType("ContractName", str),
    "Manifest": lambda: Dict[str, Any],
}

if hasattr(_upstream_module, "__all__"):
    upstream_all = set(_upstream_module.__all__)
else:
    upstream_all = None

for alias, factory in _FALLBACK_ALIASES.items():
    if hasattr(_upstream_module, alias):
        continue
    setattr(_upstream_module, alias, factory())
    if upstream_all is not None:
        upstream_all.add(alias)

if upstream_all is not None:
    _upstream_module.__all__ = tuple(upstream_all)

# Re-export everything from the upstream module so consumers continue to work
# with the genuine implementation.
globals().update(_upstream_module.__dict__)

