"""Roofing Estimator app package.

Mirrors the z_profiler pattern: monkey-patch Jinja2Templates so every
instance (the shell's plus each feature router's) exposes APP_VERSION as a
Jinja global. The footer in base.html then always matches the deployed
version, with only app/version.py to bump.
"""

from fastapi.templating import Jinja2Templates as _Jinja2Templates

from . import version as _version

_orig_init = _Jinja2Templates.__init__


def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    self.env.globals.setdefault("app_version", _version.APP_VERSION)


_Jinja2Templates.__init__ = _patched_init  # type: ignore[method-assign]
