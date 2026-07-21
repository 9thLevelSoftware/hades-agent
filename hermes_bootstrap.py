"""Backward-compatibility alias — ``hermes_bootstrap`` IS ``hades_bootstrap``."""

import sys

import hades_bootstrap

sys.modules[__name__] = hades_bootstrap
