"""Backward-compatibility alias — ``hermes_logging`` IS ``hades_logging``."""

import sys

import hades_logging

sys.modules[__name__] = hades_logging
