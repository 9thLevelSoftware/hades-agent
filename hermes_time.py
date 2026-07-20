"""Backward-compatibility alias — ``hermes_time`` IS ``hades_time``."""

import sys

import hades_time

sys.modules[__name__] = hades_time
