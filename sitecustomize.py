"""Test configuration helpers.

This module is imported automatically by Python during startup when it is
available on the import path. By setting ``PYTEST_DISABLE_PLUGIN_AUTOLOAD`` here
we ensure that running the project's test suite inside constrained environments
does not unexpectedly attempt to import every third-party pytest plugin
installed in the interpreter. Some of those plugins have additional optional
dependencies that are not present in the execution environment, which would
cause pytest to crash before any tests execute. Setting the environment
variable opts pytest out of its plugin auto-discovery behaviour, allowing the
test suite to run reliably.
"""

from __future__ import annotations

import os


# Only set the flag if it has not been defined by the user already. This keeps
# the behaviour overridable for advanced usage while providing a safe default
# for the exercises.
os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

