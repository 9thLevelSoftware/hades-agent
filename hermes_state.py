"""Backward-compatibility alias — ``hermes_state`` IS ``hades_state``."""

import sys

import hades_state

sys.modules[__name__] = hades_state
