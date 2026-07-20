"""Backward-compatibility alias — ``hermes_constants`` IS ``hades_constants``.

``sys.modules`` self-replacement (instead of a star-import shim) makes both
names resolve to the same module object, so module-level state (e.g. the
``_HADES_HOME_OVERRIDE`` ContextVar), monkeypatching, and underscore names
behave identically through either import path.
"""

import sys

import hades_constants

sys.modules[__name__] = hades_constants
