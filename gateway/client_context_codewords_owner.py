"""Fresh native owner receipts; ordinary tools still use existing approval checks."""
from dataclasses import dataclass
import weakref

from gateway.client_context_policy import require
from gateway import client_context_codewords as route

_issued = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class _Owner:
    identity: tuple
    policy: object
    adapter: object
    client: object
    bot: str
    message_id: str


def issue(adapter, source, policy, member, message_id):
    require(policy.operator_user_id == source.user_id == "U07MJJMMK9T")
    require(member.get("team_id") == route.WORKSPACE)
    require(not any(hasattr(source, name) for name in (
        "_codewords_policy", "_text_only_policy", "_text_only_native_identity")))
    receipt = _Owner(route.native_identity(source), policy, adapter,
                     adapter._team_clients[route.WORKSPACE],
                     adapter._team_bot_user_ids[route.WORKSPACE],
                     str(message_id or ""))
    _issued.add(receipt)
    source._codewords_owner = receipt
    # Bounded admission receipts must never be confused with operator receipts.
    if hasattr(source, "_codewords_native_identity"):
        del source._codewords_native_identity


def authorized(config, source, adapter=None):
    try:
        receipt = getattr(source, "_codewords_owner", None)
        require(type(receipt) is _Owner and receipt in _issued)
        require(receipt.identity == route.native_identity(source))
        require(route.route(config, source) == receipt.policy)
        require(receipt.policy.operator_user_id == source.user_id == "U07MJJMMK9T")
        require(adapter is None or receipt.adapter is adapter)
        require(receipt.adapter._team_clients.get(route.WORKSPACE) is receipt.client)
        require(receipt.adapter._team_bot_user_ids.get(route.WORKSPACE) == receipt.bot)
        require(not any(hasattr(source, name) for name in (
            "_codewords_native_identity", "_codewords_policy", "_text_only_policy",
            "_text_only_native_identity")))
        return True
    except Exception:
        return False


def event_authorized(config, event):
    return (authorized(config, event.source) and not event.internal
            and str(event.message_id or "") == event.source._codewords_owner.message_id)
