"""SPI GA4/GSC aggregate reads for Rhiz's Slack grant (EZ-1074).

The ``spi`` toolset is never a platform default; it reaches a turn only through
gateway.restricted_users.slack. The handler re-checks the bound session and the
live grant on every call, so a model argument or a stale toolset cannot widen it.
"""
from __future__ import annotations

import json

from tools.registry import registry


def _bound(var):
    from gateway.session_context import _UNSET

    value = var.get()
    return "" if value is _UNSET else value


def _rhiz_turn():
    """True only inside a bound Slack turn from Rhiz whose current grant still names spi."""
    try:
        from gateway import session_context as sc
        from gateway.client_context_restricted import USER, WORKSPACE
        from gateway.run import _load_gateway_config
        from plugins.platforms.slack.restricted_grant import grant_toolsets

        bound = (_bound(sc._SESSION_PLATFORM), _bound(sc._SESSION_USER_ID), _bound(sc._SESSION_SCOPE_ID))
        if bound != ("slack", USER, WORKSPACE):
            return False
        grant = grant_toolsets(_load_gateway_config(), USER, _bound(sc._SESSION_CHAT_ID))
        return isinstance(grant, list) and "spi" in grant
    except Exception:
        return False


def _handler(name):
    def run(args, **kw):
        from gateway.client_context_rhiz_reads import execute

        return execute(name, json.dumps(args if isinstance(args, dict) else {}), _rhiz_turn)
    return run


def _schemas():
    from gateway.client_context_rhiz_reads import schemas

    return [entry["function"] for entry in schemas()]


# Module-level loop: discover_builtin_tools only imports modules whose body calls registry.register.
for _schema in _schemas():
    registry.register(name=_schema["name"], toolset="spi", schema=_schema,
                      handler=_handler(_schema["name"]), emoji="📈")
