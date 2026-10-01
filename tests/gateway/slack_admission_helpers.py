"""Explicit task authority for transport tests that do not exercise native ingress."""
from contextlib import contextmanager

from plugins.platforms.slack.deletion import _delivery_scope
from plugins.platforms.slack.participation import _Participation, _participation_scope


@contextmanager
def slack_task(adapter, channel, requester, session_key, *, team_id="", thread_id=""):
    """Model one admitted requester task, retaining all output/deletion checks.

    Native admission itself is exercised in the participation ingress tests.
    A captured callback loses this grant when the test's task scope ends.
    """
    key = (team_id, channel, thread_id)
    grant = _Participation(adapter, key, requester, session_key,
                           adapter._deletion_generation(key))
    task_token = _participation_scope.set(grant)
    deletion_token = _delivery_scope.set((adapter, key, grant.generation))
    try:
        yield grant
    finally:
        grant.live = False
        _delivery_scope.reset(deletion_token)
        _participation_scope.reset(task_token)
