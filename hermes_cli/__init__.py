"""Backward-compatibility alias package — ``hermes_cli`` IS ``hades_cli``.

The project was renamed from hermes-agent to hades-agent, but the hermes
ecosystem (plugins, skills, user scripts, ``python -m hermes_cli.X``
invocations) still imports the old names.  This package aliases every
``hermes_cli`` submodule to its ``hades_cli`` twin at the module-object
level: ``sys.modules["hermes_cli.config"] is sys.modules["hades_cli.config"]``
holds, so state, monkeypatching, and ``isinstance`` behave identically
through either name.

Known limitation: classes still carry ``__module__ == "hades_cli.x"``, so
pickles produced here do not unpickle on a stock upstream hermes-agent
install.  Cross-fork pickle portability is out of scope.
"""

import importlib
import importlib.abc
import importlib.util
import sys

_ALIAS = "hermes_cli"
_REAL = "hades_cli"


class _AliasLoader(importlib.abc.Loader):
    """Loader that hands back the already-imported ``hades_cli`` module."""

    def __init__(self, real_name, real_spec):
        self._real_name_str = real_name
        self._real_spec = real_spec

    def create_module(self, spec):
        return importlib.import_module(self._real_name_str)

    def exec_module(self, module):
        # module_from_spec() just stamped the alias identity (__name__,
        # __spec__, __loader__, __package__) onto the *shared* hades module;
        # restore the canonical attributes so logging namespaces, repr, and
        # relative imports keep resolving against the real package.
        spec = self._real_spec
        if spec is not None:
            module.__name__ = spec.name
            module.__spec__ = spec
            module.__loader__ = spec.loader
            module.__package__ = spec.parent

    def _to_real(self, fullname):
        return _REAL + fullname[len(_ALIAS):]

    # runpy (``python -m hermes_cli.X``) requires code access; delegate to the
    # real module's loader so execution matches ``python -m hades_cli.X``.
    def get_code(self, fullname):
        return self._real_spec.loader.get_code(self._to_real(fullname))

    def get_source(self, fullname):
        return self._real_spec.loader.get_source(self._to_real(fullname))


class _AliasFinder(importlib.abc.MetaPathFinder):
    # Marker checked instead of isinstance(): a re-import of this file after a
    # sys.modules purge defines a fresh class, and isinstance against it would
    # miss the finder installed by the previous execution.
    _hermes_cli_alias_finder = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _ALIAS and not fullname.startswith(_ALIAS + "."):
            return None
        real_name = _REAL + fullname[len(_ALIAS):]
        try:
            real_spec = importlib.util.find_spec(real_name)
        except (ImportError, ModuleNotFoundError, ValueError):
            return None
        if real_spec is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(real_name, real_spec),
            is_package=real_spec.submodule_search_locations is not None,
        )


# Must sit ahead of PathFinder: sys.modules["hermes_cli"] is replaced with
# hades_cli below, so PathFinder would otherwise resolve hermes_cli.X through
# hades_cli.__path__ into a fresh duplicate module object under the alias name.
if not any(
    getattr(f, "_hermes_cli_alias_finder", False) for f in sys.meta_path
):
    sys.meta_path.insert(0, _AliasFinder())

import hades_cli

sys.modules[__name__] = hades_cli
