"""Project-wide site customization hooks."""

import os

# Disable auto-loading external pytest plugins to keep the test environment deterministic.
os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
