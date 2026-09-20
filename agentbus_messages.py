"""Publish and render envelopes shared by shell, hooks, and MCP clients."""

import agentbus_context as context
import collections
import os
import time
import uuid

import agentbus_constants as constants
import agentbus_identity as identity
import agentbus_presence as presence
import agentbus_routing as routing
import agentbus_state as state_store
import agentbus_storage as storage

import project_confirmation

_PrepareOptions = collections.namedtuple(
    "PrepareOptions", 'kind task_id reply_to job broadcast reply_context',
    defaults=('message', None, None, None, False, None))

_DirectOptions = collections.namedtuple(
    "DirectOptions",
    'kind task_id reply_to job broadcast return_record reply_context',
    defaults=(
        'message',
        None,
        None,
        None,
        False,
        False,
        None))

_SendOptions = collections.namedtuple(
    "SendOptions", 'kind task_id reply_to job broadcast return_record',
    defaults=('message', None, None, None, False, False))


@context.locked
def send(bus, sender, to, text, *args, **kwargs):
    """Find a related window and confirm its project before sharing content.

    The first send holds the content and asks a project-only question. Once
    the two windows establish a project relationship, later communication
    can proceed while both session/job/task contexts remain unchanged.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        sender (str): CLI name publishing the message.
        to (str): CLI name, full handle, or explicit broadcast wildcard.
        text (str): Message body to publish.
        args (object): Optional compatibility settings in their documented
            order.
        **kwargs (object): Optional routing and response settings by name.
    """
    options = _SendOptions(*args, **kwargs)
    record = prepare_message(
        bus,
        sender,
        to,
        text,
        options.kind,
        options.task_id,
        options.reply_to,
        options.job,
        options.broadcast)
    if options.kind in ("message", "task", "result"):
        record = project_confirmation.request_confirmation(bus, sender, record)
    else:
        storage.append(bus, record)
        storage.compact(bus)
    return record if options.return_record else record["id"]


def format_messages(messages):
    """Render messages as plain text for injection into a session.

    Age is on every line so a reader can see at a glance whether a message
    belongs to what is happening now.

    Args:
        messages (list[dict]): Message envelopes delivered to this window.

    Returns:
        str: Message block suitable for a hook or terminal.
    """
    lines = []
    for message in messages:
        sender = message.get("from_handle") or message.get("from", "?")
        age = int(time.time() - message.get("ts", time.time()))
        # A receipt has no body worth printing: who read what, and when.
        if message.get("kind") == "ack":
            lines.append(
                (
                    f'[ack] {
                    sender!s} read your message ' f'{
                    message.get(
                    'reply_to',
                    '?')!s} ({
                    max(
                    age,
                    0):d}s ago)'
                ))
            lines.append("")
            continue
        header = f'[{message.get('kind', 'message')!s}] from {sender!s}'
        if message.get("task_id"):
            header += f' ({message['task_id']!s})'
        header += f' id={message.get('id', '?')!s}'
        header += f' ({max(age, 0):d}s ago)'
        lines.append(header)
        lines.append(message.get("text", ""))
        lines.append("")
    return "\n".join(lines).strip()


def watch(bus, on_message):
    """Follow every message crossing the bus without consuming any of it.

    Tails the file from its end. It keeps no cursor of its own, so
    watching never affects what an agent receives.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        on_message (callable): Callback receiving each newly appended envelope.
    """
    offset = os.path.getsize(bus.path)
    while True:
        entries, offset = storage.scan(bus, offset)
        for record, _end in entries:
            on_message(record)
        time.sleep(0.4)


def prepare_message(bus, sender, to, text, *args, **kwargs):
    """Prepare an envelope for a related window or explicit broadcast.

    Bare CLI destinations select a matching task or job. Full handles are
    direct destinations. Replies prefer the original sending session.
    broadcast=True reaches every window of the chosen CLI, or every agent
    with to="*". The chosen session is fixed when sending, so renaming it
    or reusing its handle cannot redirect queued mail.

    Returns the complete envelope without appending its contents.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        sender (str): CLI name publishing the message.
        to (str): CLI name, full handle, or explicit broadcast wildcard.
        text (str): Message body to publish.
        args (object): Optional compatibility settings in their documented
            order.
        **kwargs (object): Optional routing and response settings by name.

    Returns:
        dict: Validated envelope before publication.
    """
    options = _PrepareOptions(*args, **kwargs)
    state_store.check_name(sender)
    if to != "*":
        state_store.check_name(to)
    elif not options.broadcast:
        raise ValueError("'*' requires an explicit broadcast")
    if options.kind not in constants.MESSAGE_KINDS:
        raise ValueError((
            f'invalid kind {options.kind!r}: expected one of '
            f'{', '.join(constants.MESSAGE_KINDS)!s}'
        ))
    if not text or not text.strip():
        raise ValueError("message text is required")
    presence.touch(bus, sender)
    if options.broadcast:
        known = set(constants.AGENT_NAMES) | {
            row["agent"] for row in presence.routing_rows(bus)}
        if to != "*" and to not in known:
            raise ValueError("broadcast target must be a CLI name or '*'")
        route = {"to": to, "broadcast": True, "routing": "broadcast"}
    else:
        route = routing.resolve_recipient(
            bus,
            sender,
            to,
            reply_to=options.reply_to,
            task_id=options.task_id if options.kind == "result" else None,
            reply_context=options.reply_context)
    record = {"id": uuid.uuid4().hex[:12],
              "ts": time.time(),
              "from": sender,
              "kind": options.kind,
              "text": text,
              "job": options.job or bus.job,
              "from_handle": state_store.current_handle(bus,
                                                        sender),
              "from_session": bus.session,
              "requested_to": to}
    record.update(route)
    for direction in ("from", "to"):
        generation = identity.session_generation(
            record.get(direction + "_session"))
        if generation is not None and record.get(
                direction + "_session_start") is None:
            record[direction + "_session_start"] = generation
    if options.task_id:
        record["task_id"] = options.task_id
    if options.reply_to:
        record["reply_to"] = options.reply_to

    return record


def send_direct(bus, sender, to, text, *args, **kwargs):
    """Internal transport for control messages and already-authorized mail.

    Args:
        bus (Bus): Connection whose shared bus state is used.
        sender (str): CLI name publishing the message.
        to (str): CLI name, full handle, or explicit broadcast wildcard.
        text (str): Message body to publish.
        args (object): Optional compatibility settings in their documented
            order.
        **kwargs (object): Optional routing and response settings by name.

    Returns:
        str or dict: Published id or requested envelope.
    """
    options = _DirectOptions(*args, **kwargs)
    record = prepare_message(
        bus,
        sender,
        to,
        text,
        options.kind,
        options.task_id,
        options.reply_to,
        options.job,
        options.broadcast,
        options.reply_context)
    storage.append(bus, record)
    storage.compact(bus)
    return record if options.return_record else record["id"]
