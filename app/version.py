"""Single source of truth for the app version. Bump on each ship.

Injected into every Jinja env as `app_version` (see app/__init__.py) so the
footer always reflects the running build.
"""

APP_VERSION = "1.0.0"
