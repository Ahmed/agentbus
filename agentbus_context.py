"""Serialize project changes and releases across all bus entry points."""

import fcntl
import functools
import os
import sys
import threading

import agentbus_state as state

# Long-lived integrations and shell entry points must share nested flock
# ownership even when they load the bus and confirmation modules separately.
CONTEXT_LOCKS = (getattr(sys.modules.get("bus"), "_CONTEXT_LOCKS", None)
                 or getattr(sys.modules.get("__main__"), "_CONTEXT_LOCKS", None)
                 or threading.local())


def _locked_call(function, client, *args, **kwargs):
    """Allow nested operations while keeping separate threads serialized.

    Args:
        function (callable): Operation protected by the shared context lock.
        client (Bus): Connection whose project state must remain stable.
        *args (object): Positional operation arguments.
        **kwargs (object): Keyword operation arguments.

    Returns:
        object: Result of the protected operation.
    """
    path = os.path.realpath(os.path.join(client.state, "project_context.lock"))
    held = getattr(CONTEXT_LOCKS, "held", None)
    if held is None:
        held = CONTEXT_LOCKS.held = set()
    if path in held:
        return function(client, *args, **kwargs)
    with open(path, "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        held.add(path)
        try:
            # The connection may predate another process's project change.
            client.handle = state.read_handle(client.state, client.session)
            client.job = (os.environ.get("AGENTBUS_JOB")
                          or state.read_job(client.state, client.session,
                                            client.cwd)
                          or client.job)
            return function(client, *args, **kwargs)
        finally:
            held.remove(path)


def locked(function):
    """Keep context changes and confirmation releases inside the same lock.

    Args:
        function (callable): Bus operation whose first argument is a client.

    Returns:
        callable: Reentrant wrapper preserving the operation's metadata.
    """
    return functools.wraps(function)(functools.partial(_locked_call, function))
