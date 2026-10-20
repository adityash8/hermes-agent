"""Per-sender Slack tool grant from gateway.restricted_users.

The ordinary agent serves the turn. This only narrows the tool list so a
restricted sender cannot inherit the full Slack toolset (memory, cron, MCP).
"""
from __future__ import annotations


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
    if not isinstance(entry, dict):
        entry = slack.get("_default")
    if not isinstance(entry, dict):
        return None
    enabled = entry.get("enabled_toolsets")
    if not isinstance(enabled, list):
        return None
    names = []
    for item in enabled:
        if not isinstance(item, str) or not item.strip() or item in names:
            continue
        names.append(item.strip())
    return names
