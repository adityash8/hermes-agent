"""Per-sender Slack tool grant from gateway.restricted_users.

The ordinary agent serves the turn. This only narrows the tool list so a
restricted sender cannot inherit the full Slack toolset (memory, cron, MCP).

A configured sender fails closed: a malformed grant yields no tools, and a
grant that names no enabled MCP server gets the ``no_mcp`` sentinel so the
resolver cannot fall back to every globally enabled server (composio et al.).
``enabled_toolsets: null`` (or omitted) still means the platform default.
"""
from __future__ import annotations

NO_MCP = "no_mcp"


def _allowlisted_mcp_servers(names, servers):
    """MCP servers the resolver will treat as an explicit allowlist for these names.

    Mirrors _get_platform_tools: a name that is also a configurable, plugin or
    platform-default toolset key never counts as an MCP passthrough there.
    """
    from hermes_cli.tools_config import _configurable_keys, _get_plugin_toolset_keys, _platform_default_keys

    shadowed = _configurable_keys() | _get_plugin_toolset_keys() | _platform_default_keys()
    return {n for n in names if n not in shadowed} & servers


def _fence_mcp(config, names):
    """Listed MCP servers stay an allowlist; none listed means none at all."""
    from hermes_cli.tools_config import enabled_mcp_server_names

    servers = enabled_mcp_server_names(config)
    if NO_MCP in names or not servers or _allowlisted_mcp_servers(names, servers):
        return names
    return names + [NO_MCP]


def grant_toolsets(config, user_id, channel_id):
    """Return the explicit toolset list for this sender, or None for the platform default."""
    if not isinstance(config, dict):
        return None
    slack = ((config.get("gateway") or {}).get("restricted_users") or {}).get("slack") or {}
    if not isinstance(slack, dict):
        return None
    # Precedence: explicit channel > explicit sender > ``_default``. An explicit empty list is a
    # zero-tool grant and never falls through. ``_``-prefixed keys are reserved, never sender ids.
    channels = slack.get("_channels") or {}
    entry = channels.get(channel_id) if isinstance(channels, dict) and channel_id else None
    if not isinstance(entry, dict) and user_id and not str(user_id).startswith("_"):
        entry = slack.get(user_id)
        if entry is not None and not isinstance(entry, dict):
            return []
    if not isinstance(entry, dict):
        entry = slack.get("_default")
    if not isinstance(entry, dict):
        return None
    enabled = entry.get("enabled_toolsets")
    if enabled is None:
        return None
    if not isinstance(enabled, list):
        return []
    names = []
    for item in enabled:
        if not isinstance(item, str) or not item.strip() or item.strip() in names:
            continue
        names.append(item.strip())
    return _fence_mcp(config, names) if names else names
