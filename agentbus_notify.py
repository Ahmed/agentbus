"""Ring a window the moment mail lands, instead of waiting for a poll.

Redis is required, and this is the module that requires it.

A doorbell, not a mailbox. Redis carries no message contents and holds
no state: every ring says only "look at the bus", and the file remains
the one place a message actually lives.

That division is what makes the dependency safe rather than fatal. A
Redis that is down, unreachable or missing costs latency and nothing
else, because a reader that misses a ring still finds the mail by
looking. Read that as a guarantee that a broken Redis cannot take the
bus down, not as permission to run without one -- the paragraph below is
what stops working when it is absent.

Why it exists. A window sitting at an empty prompt fires no hooks, so
nothing tells it mail arrived; the watcher closes that gap by polling
every couple of seconds and ringing the terminal. Polling is the part
this replaces. A subscriber blocks until the instant a message is
appended, which turns "up to two seconds late, having woken up twenty
times to find nothing" into "immediately, having slept the whole time".

The far more useful consequence is that a blocking wait is something a
CLI can be made to do. A hook that waits here keeps a turn alive while
it listens, and a shell command that waits here exits the moment mail
arrives -- which, for a CLI that notices a background command finishing,
is the only way anything has ever reached an idle window without the
operator typing.

Every entry point here is best effort and swallows its own failures.
Nothing in this module is allowed to be the reason a message fails to
send.
"""

import os
import time

import agentbus_constants as constants

try:
    import redis as redis_client
except ImportError:
    redis_client = None


# Where the doorbell lives. Only ever used for pub/sub, so pointing this
# at a shared Redis is also what makes a window on another machine
# reachable.
#
# Database 5 to stay clear of whatever else is on this server; 0 and 4
# are already in use here. Note that the number is a courtesy rather
# than a wall: Redis pub/sub is not scoped to a database, so channels
# are visible across all of them whatever this says. CHANNEL_PREFIX is
# the thing actually keeping the bus from colliding with anyone else.
REDIS_URL = os.environ.get("AGENTBUS_REDIS_URL",
                           "redis://127.0.0.1:6379/5")

# Set AGENTBUS_REDIS=0 to switch the doorbell off entirely and leave
# every reader on the polling path. Mail still arrives; delivery to a
# window nobody is typing into does not.
REDIS_ENV = "AGENTBUS_REDIS"

# Kept short on purpose. This runs immediately after a message is
# appended, so a Redis that has stopped answering must cost the sender a
# moment rather than the send.
SOCKET_TIMEOUT_SECONDS = 0.5

# Namespace for every channel, so a Redis shared with an application
# cannot collide with the bus.
CHANNEL_PREFIX = "agentbus"

_CONNECTION = {"client": None, "tried": False}
_SUBSCRIBER = {"client": None, "tried": False}


def enabled():
    """Is the doorbell switched on and importable?

    Returns:
        bool: False when the library is absent or it is switched off, in
            which case every other call here is a no-op.
    """
    if redis_client is None:
        return False
    return os.environ.get(REDIS_ENV, "1") != "0"


def _client():
    """The shared connection, opened once and reused.

    A failure to connect is remembered rather than retried on every
    message: the common reason to be here with no Redis is that there is
    no Redis, and retrying per send would add its timeout to each one.

    Returns:
        Redis or None: The client, or None when unavailable.
    """
    if not enabled():
        return None
    if _CONNECTION["tried"]:
        return _CONNECTION["client"]

    _CONNECTION["tried"] = True
    try:
        client = redis_client.Redis.from_url(
            REDIS_URL,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS)
        client.ping()
    except (OSError, ValueError, AttributeError,
            redis_client.RedisError):
        return None
    _CONNECTION["client"] = client
    return client


def reset():
    """Forget both cached connections.

    A connection failure is remembered deliberately, so a process that
    started without Redis does not pay a timeout on every send. That
    memory has to be clearable by anything that changes the environment
    underneath it -- which in practice means the tests.
    """
    _CONNECTION.update(client=None, tried=False)
    _SUBSCRIBER.update(client=None, tried=False)


def available():
    """Is a Redis actually reachable right now?

    Distinct from enabled(), which only answers whether the doorbell is
    switched on and importable. This one opens the connection and asks.

    Returns:
        bool: True when a server answered.
    """
    return _client() is not None


def channels_for(agent, session=None):
    """The channels a window listens on.

    Two of them, because a message is addressed more precisely than a
    window can always be found. A message with a resolved destination
    session rings only that window; one queued for a handle that has not
    registered yet can only be aimed at the CLI name, and every window of
    that CLI wakes and looks.

    Args:
        agent (str): CLI name the window answers to.
        session (str or None): That window's session id, when known.

    Returns:
        list[str]: Channel names to subscribe to, most specific first.
    """
    names = []
    if session:
        names.append(f"{CHANNEL_PREFIX}:session:{session}")
    if agent:
        names.append(f"{CHANNEL_PREFIX}:agent:{agent}")
    return names


def ring(record):
    """Tell whoever the record is addressed to that something arrived.

    Called after the append rather than during it, so a slow or dead
    Redis cannot be holding the bus file's lock while it times out.

    The ring carries the record's kind and nothing else. A listener is
    expected to look at the bus for the substance, which is what keeps a
    missed ring harmless.

    Args:
        record (dict): The message that was just appended.
    """
    client = _client()
    if client is None:
        return

    agent = record.get("to_agent") or record.get("to") or ""
    if agent == constants.BROADCAST_JOB:
        agent = ""
    names = channels_for(agent, record.get("to_session"))
    if not names:
        return

    kind = record.get("kind") or "message"
    for name in names:
        try:
            client.publish(name, kind)
        except (OSError, redis_client.RedisError):
            return


def _subscriber():
    """A connection that is allowed to sit still and listen.

    Separate from the publishing client on purpose: that one carries a
    half-second socket timeout so a dead Redis cannot delay a send, and
    a subscriber waiting minutes would trip over exactly that.

    Returns:
        Redis or None: The client, or None when unavailable.
    """
    if not enabled():
        return None
    if _SUBSCRIBER["tried"]:
        return _SUBSCRIBER["client"]

    _SUBSCRIBER["tried"] = True
    try:
        client = redis_client.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS)
        client.ping()
    except (OSError, ValueError, AttributeError,
            redis_client.RedisError):
        return None
    _SUBSCRIBER["client"] = client
    return client


def wait_or_sleep(agent, session, timeout, floor):
    """Wait for a ring, and never return faster than a poll would.

    The guarantee callers actually need. wait() reports "no ring" the
    instant it cannot reach a doorbell, which is the right answer and a
    terrible thing to put in a loop: a caller that only sleeps when the
    doorbell is switched *off* will spin at full tilt when it is
    switched on and merely unreachable -- library installed, server
    down, which is the ordinary way Redis fails.

    Keying the fallback on elapsed time rather than on configuration
    covers every one of those: disabled, missing, refused, dropped
    mid-wait.

    Args:
        agent (str): CLI name the window answers to.
        session (str or None): That window's session id, when known.
        timeout (float): Longest time to wait for a ring.
        floor (float): Least time to have spent before returning without
            one, so the caller's loop cannot become a spin.

    Returns:
        bool: True only if a ring actually arrived.
    """
    started = time.time()
    if wait(agent, session, timeout):
        return True
    spent = time.time() - started
    if spent < floor:
        time.sleep(min(floor - spent, max(timeout, 0.0)))
    return False


def wait(agent, session, timeout):
    """Block until somebody rings for this window, or the time runs out.

    Args:
        agent (str): CLI name the window answers to.
        session (str or None): That window's session id, when known.
        timeout (float): Longest time to wait, in seconds.

    Returns:
        bool: True if a ring arrived. False on a timeout, and also
            whenever the doorbell is unavailable -- callers treat both
            the same way, by going back to looking for themselves.
    """
    client = _subscriber()
    if client is None:
        return False

    names = channels_for(agent, session)
    if not names:
        return False

    deadline = time.time() + max(0.0, float(timeout))
    pubsub = None
    try:
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(*names)
        # Looped rather than waited once. The first thing to arrive is
        # the subscribe confirmation, which is ignored and comes back as
        # None -- indistinguishable from a timeout unless we keep asking
        # until the clock actually runs out.
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            if pubsub.get_message(timeout=remaining) is not None:
                return True
    except (OSError, ValueError, redis_client.RedisError):
        return False
    finally:
        if pubsub is not None:
            try:
                pubsub.close()
            except (OSError, redis_client.RedisError):
                pass
