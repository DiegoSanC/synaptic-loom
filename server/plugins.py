"""Server plugins (D21): installed distributions that add routes to the server.

A plugin declares an entry point in the group `loom.server_plugins` whose object is a
callable `register(app)`. `server.app` calls `load` once, after every core route is
declared. A plugin that cannot be loaded, raises, or declares a method and path the server
already answers stops the server from starting: a half-loaded server, or a plugin route
silently shadowed by an earlier one, is worse than no plugin.
"""
from importlib.metadata import entry_points

from fastapi import FastAPI

GROUP = "loom.server_plugins"


def _answered(app: FastAPI) -> set[tuple[str, str]]:
    return {(method, route.path)
            for route in app.routes for method in getattr(route, "methods", None) or ()}


def load(app: FastAPI) -> list[str]:
    """Register every installed plugin on `app`, in entry-point-name order; return the names."""
    names = []
    for entry_point in sorted(entry_points(group=GROUP), key=lambda ep: ep.name):
        try:
            register = entry_point.load()
        except Exception as exc:
            raise RuntimeError(f"server plugin {entry_point.name!r} could not be loaded: "
                               f"{exc}") from exc
        before = _answered(app)
        routes = len(app.routes)
        try:
            register(app)
        except Exception as exc:
            raise RuntimeError(f"server plugin {entry_point.name!r} failed to register: "
                               f"{exc}") from exc
        for route in app.routes[routes:]:
            for method in sorted(getattr(route, "methods", None) or ()):
                if (method, route.path) in before:
                    raise RuntimeError(f"server plugin {entry_point.name!r} declares {method} "
                                       f"{route.path}, which the server already answers")
        names.append(entry_point.name)
    return names
