"""Withhold message contents until the addressed window confirms its project.

Checks and status notices contain project and delivery metadata only. Held
payloads live in private pending files, never in the shared log, until an
explicit yes. A confirmed pair can communicate in either direction while
both session, job and task identities remain the same.
"""

import fcntl
import hashlib
import json
import os
import re
import shlex
import time
import uuid

import agentbus_constants as constants
import agentbus_context as context_lock
import agentbus_identity as identity
import agentbus_presence as presence
import agentbus_routing as routing
import agentbus_state as state
import agentbus_storage as storage

_CONFIRMATION_ID = re.compile(r"^[a-f0-9]{12}$")
_FINAL_STATUSES = ("confirmed", "rejected", "expired")


def _new_id():
    """A short, unguessable id for one held request."""
    return uuid.uuid4().hex[:12]


def _write_json(path, record):
    """Atomically replace state, readable only by its owner."""
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_json(path):
    """The record stored at this path, or None if it is absent or junk."""
    try:
        with open(path, encoding="utf-8") as source:
            record = json.load(source)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _pending_path(client, confirmation_id):
    """Where one held request lives, refusing anything not an id.

    Args:
        client (Bus): Connection whose private state holds the request.
        confirmation_id (str): The id to place.

    Returns:
        str: Path to the pending file.

    Raises:
        ValueError: The id is not twelve hex characters, and so could
            name a file outside the state directory.
    """
    if (not isinstance(confirmation_id, str)
            or not _CONFIRMATION_ID.fullmatch(confirmation_id)):
        raise ValueError("invalid project confirmation id")
    return os.path.join(client.state, f"pending.{confirmation_id}.json")


def pending_state(client, confirmation_id):
    """What became of one held request, without disturbing it.

    Read-only and non-consuming, so a sender may ask repeatedly while it
    waits for an answer. Only the status and the reason are exposed:
    the held payload is nobody's business but the addressed window's,
    including the sender's.

    Args:
        client (Bus): Connection whose private state holds the request.
        confirmation_id (str): The id to look up.

    Returns:
        dict or None: {"status", "reason"} for the request, or None when
            there is no such request -- expired and swept, or never made.
    """
    try:
        record = _load_json(_pending_path(client, confirmation_id))
    except ValueError:
        return None
    if record is None:
        return None
    return {"status": record.get("status"), "reason": record.get("reason")}


def _context(client, session, agent, handle, job):
    generation = identity.session_generation(session)
    canonical = identity.canonical_session(client, session, generation)
    context = {"session": canonical, "agent": agent, "job": job,
               "task": routing.task_stem(handle, agent, session)}
    if generation is not None and canonical == session:
        context["session_start"] = generation
    return context


def _local_context(client, agent):
    return _context(client, client.session, agent,
                    state.current_handle(client, agent), client.job)


def _current_context(client, session, agent, generation=None):
    """Read a peer's current task without interpreting its message contents."""
    canonical = identity.canonical_session(client, session, generation)
    contexts = [_context(client, row["session"], agent, row["handle"],
                         row.get("job"))
                for row in presence.routing_rows(client)
                if row["agent"] == agent]
    contexts = [
        context for context in contexts if context["session"] == canonical]
    if not contexts:
        return None
    # A legacy and canonical presence row can briefly overlap during binding.
    if any(item != contexts[0] for item in contexts[1:]):
        return None
    return contexts[0]


def _canonical_context(client, context):
    result = dict(context)
    session = result["session"]
    generation = result.get("session_start")
    # Old process-keyed state has no proof of which PID generation agreed.
    # Keep it distinct rather than inheriting the current process's binding.
    if generation is not None or not re.fullmatch(
            r"(claude|codex|gemini|node)[0-9]+", session):
        result["session"] = identity.canonical_session(
            client, session, generation)
    if result["session"] != session:
        result.pop("session_start", None)
    return result


def _pair(contexts):
    ordered = sorted(
        contexts,
        key=lambda item: (
            item["session"],
            item["agent"]))
    identities = [(item["session"], item["agent"], item.get("session_start"))
                  for item in ordered]
    key = hashlib.sha256(json.dumps(identities).encode("utf-8")).hexdigest()
    return key, ordered


def _migrate_links(client, links):
    """Move stored pairs whose sessions have since become canonical.

    A pair recorded under an old process-keyed session would otherwise
    never be found again by a window that has since bound a real session
    id, and the approval would look absent rather than moved.

    Args:
        client (Bus): Connection whose shared state holds the links.
        links (dict): The stored relationships, rewritten in place.

    Returns:
        bool: True when anything moved, so the caller knows to store it.
    """
    migrated = False
    for previous_key, previous in list(links.items()):
        if (not isinstance(previous, dict)
                or len(previous.get("contexts", [])) != 2):
            continue
        canonical = [_canonical_context(client, item)
                     for item in previous["contexts"]]
        canonical_key, canonical = _pair(canonical)
        if (canonical_key == previous_key
                and canonical == previous["contexts"]):
            continue
        links.pop(previous_key, None)
        candidate = dict(previous, contexts=canonical)
        existing = links.get(canonical_key, {})
        if candidate.get("confirmed_at", 0) >= existing.get("confirmed_at", 0):
            links[canonical_key] = candidate
        migrated = True
    return migrated


def _relationship(client, first, second, confirmation_id=None, revoke=False):
    """Check or establish one bidirectional relationship under a real lock.

    Each session pair keeps only its current contexts. Observing a changed
    job or task invalidates the old approval instead of letting it revive
    when a later message returns to an earlier context.
    """
    if first is None or second is None:
        return False
    first = _canonical_context(client, first)
    second = _canonical_context(client, second)
    key, contexts = _pair((first, second))
    path = os.path.join(client.state, "project_links.json")
    with open(path + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        links = _load_json(path) or {}
        if _migrate_links(client, links):
            _write_json(path, links)
        entry = links.get(key)
        if revoke:
            if entry is not None:
                links.pop(key, None)
                _write_json(path, links)
            return False
        matches = isinstance(entry, dict) and entry.get("contexts") == contexts
        if confirmation_id is not None:
            links[key] = {
                "contexts": contexts,
                "confirmation_id": confirmation_id,
                "confirmed_at": time.time()}
            _write_json(path, links)
            return True
        if entry is not None and not matches:
            links.pop(key, None)
            _write_json(path, links)
        return matches


def invalidate_relationships(client):
    """Forget approvals involving this window after its job or task changes."""
    session = identity.canonical_session(client, client.session)
    path = os.path.join(client.state, "project_links.json")
    with open(path + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        links = _load_json(path) or {}
        stale = [
            key for key,
            entry in links.items() if isinstance(
                entry,
                dict) and any(
                _canonical_context(
                    client,
                    context)["session"] == session for context in entry.get(
                    "contexts",
                    []))]
        for key in stale:
            links.pop(key, None)
        if stale:
            _write_json(path, links)
        return len(stale)


def _task_status(client, payload, status, confirmation_id=None):
    """Advance delegation state only once its contents are released."""
    task_id = payload.get("task_id")
    if not task_id or payload.get("kind") not in ("task", "result"):
        return
    if payload["kind"] == "result":
        fields = {"result_confirmation_status": status,
                  "result_confirmation_id": confirmation_id,
                  "result_message_id": payload["id"]}
        if status in ("confirmed", "queued"):
            fields["status"] = "done"
    else:
        current = storage.get_task(client, task_id)
        task_state = "sent" if status in ("confirmed", "queued") else status
        if task_state == "sent" and current.get(
                "status") in ("delivered", "done"):
            task_state = current["status"]
        fields = {
            "status": task_state,
            "confirmation_status": status,
            "confirmation_id": confirmation_id,
            "sender": payload["from"],
            "sender_handle": payload.get("from_handle"),
            "sender_session": payload.get("from_session"),
            "sender_session_start": payload.get("from_session_start"),
            "assignee": payload["to"],
            "assignee_handle": payload["to"],
            "assignee_session": payload.get("to_session"),
            "assignee_session_start": payload.get("to_session_start"),
            "assignee_agent": payload.get("to_agent"),
            "requested_assignee": payload.get(
                "requested_to",
                payload["to"]),
            "message_id": payload["id"]}
    storage.record_task(client, task_id, **fields)


def _already_appended(client, message_id):
    # A reader may have consumed and compacted the line after an append but
    # before the pending state was updated. The delivery ledger covers that
    # retry window for as long as the request can still be confirmed.
    return (routing.find_message(client, message_id) is not None
            or message_id in storage.load_delivery(client))


def _append_once(client, record):
    if not _already_appended(client, record["id"]):
        storage.append(client, record)
        storage.compact(client)


def _metadata(record):
    return {key: value for key, value in record.items() if key != "text"}


def _project(client, record):
    project = record.get("job")
    if not project or project == constants.BROADCAST_JOB:
        return client.job
    return project


def _probe(client, sender, record, confirmation_id, project):
    task = routing.task_stem(record.get("from_handle"), sender,
                             record["from_session"])
    description = f" Sender task: {task!r}." if task else ""
    receiver = record.get("to_agent") or record["to"]
    script = shlex.quote(os.path.join(os.path.dirname(__file__), "bus.py"))
    command = (f"python3 {script} confirm {shlex.quote(receiver)} "
               f"{shlex.quote(confirmation_id)} yes")
    asker = record.get("from_handle", sender)
    question = (
        f"Are you working on project {project!r}?{description} {asker} "
        "wants to share a message; its contents are withheld until you "
        "explicitly confirm. Reply yes only if this window is working on "
        f"that project, otherwise no. Confirm with: {command}. To decline, "
        "replace yes with no. Alternatively use the MCP confirm_project "
        f"tool with confirmation_id={confirmation_id!r} and accept=true or "
        "false."
    )
    check = {
        "id": confirmation_id,
        "ts": time.time(),
        "from": sender,
        "from_handle": record.get(
            "from_handle",
            state.current_handle(
                client,
                sender)),
        "from_session": record["from_session"],
        "to": record["to"],
        "kind": "project_check",
            "text": question,
            "job": project,
            "project": project,
            "confirmation_id": confirmation_id,
            "requested_to": record.get(
                "requested_to",
                record["to"]),
        "routing": "project_confirmation",
    }
    for key in (
        "to_session",
        "to_session_start",
        "to_agent",
            "from_session_start"):
        if record.get(key):
            check[key] = record[key]
    return check


def _request_one(client, sender, record):
    payload = dict(record)
    payload.pop("broadcast", None)
    source_context = _local_context(client, sender)
    project = _project(client, payload)
    target_context = None
    if payload.get("to_session") and payload.get("to_agent"):
        target_context = _current_context(
            client,
            payload["to_session"],
            payload["to_agent"],
            payload.get("to_session_start"))
    if (project == source_context["job"]
            and _relationship(client, source_context, target_context)):
        payload["ts"] = time.time()
        if payload["kind"] == "task":
            _task_status(client, payload, "queued")
        storage.append(client, payload)
        storage.compact(client)
        if payload["kind"] == "result":
            _task_status(client, payload, "queued")
        result = _metadata(payload)
        result.update(status="queued", confirmations=[])
        return result

    confirmation_id = _new_id()
    check = _probe(client, sender, payload, confirmation_id, project)
    pending = {
        "version": 1, "confirmation_id": confirmation_id,
        "status": "awaiting_confirmation", "created_at": check["ts"],
        "expires_at": check["ts"] + constants.MESSAGE_TTL_SECONDS,
        "project": project, "source_context": source_context,
        "payload": payload, "probe": check,
    }
    path = _pending_path(client, confirmation_id)
    with open(path + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        _write_json(path, pending)
        _task_status(client, payload, "awaiting_confirmation", confirmation_id)
        storage.append(client, check)
        storage.compact(client)
    result = _metadata(payload)
    result.update(
        status="awaiting_confirmation",
        confirmation_id=confirmation_id)
    result["confirmations"] = [{"to": payload["to"], "id": payload["id"],
                                "confirmation_id": confirmation_id,
                                "status": "awaiting_confirmation"}]
    return result


@context_lock.locked
def request_confirmation(client, sender, record):
    """Append a metadata-only question, or send through an unchanged approval.

    ``record`` is a fully resolved envelope that the caller has not appended.
    Explicit broadcasts become separate pinned messages so one recipient's
    confirmation can never authorize disclosure to another window.
    """
    if record.get("kind") not in ("message", "task", "result"):
        raise ValueError(
            "project confirmation applies to messages, tasks and results")
    if record.get("from") != sender or not record.get("from_session"):
        raise ValueError(
            "project confirmation requires the sending window identity")
    if not record.get("broadcast"):
        return _request_one(client, sender, record)

    windows = {}
    sender_session = identity.canonical_session(client, record["from_session"])
    for row in presence.routing_rows(client):
        session = identity.canonical_session(client, row["session"])
        if session == sender_session:
            continue
        if record["to"] not in ("*", row["agent"]):
            continue
        windows[(session, row["agent"])] = row
    if not windows:
        raise ValueError("no registered recipient windows for this broadcast")

    confirmations = []
    for (session, agent), row in sorted(windows.items()):
        payload = dict(record)
        payload.update(
            id=_new_id(),
            to=row["handle"],
            to_session=session,
            to_agent=agent)
        generation = identity.session_generation(session)
        if generation is not None:
            payload["to_session_start"] = generation
        payload.pop("broadcast", None)
        result = _request_one(client, sender, payload)
        confirmations.append({"to": result["to"], "id": result["id"],
                              "confirmation_id": result.get("confirmation_id"),
                              "status": result["status"]})
    result = _metadata(record)
    result["confirmations"] = confirmations
    queued = all(item["status"] == "queued" for item in confirmations)
    result["status"] = "queued" if queued else "awaiting_confirmation"
    return result


def _status_notice(client, agent, pending):
    payload = pending["payload"]
    status = pending["status"]
    outcome = {"confirmed": "confirmed; the held message was queued",
               "rejected": "rejected; no message contents were shared",
               "expired": "expired; no message contents were shared"}[status]
    return {
        "id": _new_id(),
        "ts": time.time(),
        "from": agent,
        "from_handle": state.current_handle(
            client,
            agent),
        "from_session": client.session,
        "to": payload.get(
            "from_handle",
            payload["from"]),
        "to_session": identity.canonical_session(
            client,
            payload["from_session"],
            payload.get("from_session_start")),
        "to_session_start": payload.get("from_session_start"),
        "to_agent": payload["from"],
        "kind": "project_status",
                "text": (
                    f"Project confirmation {
                        pending['confirmation_id']} " f"{outcome}."),
        "job": pending["project"],
        "project": pending["project"],
        "confirmation_id": pending["confirmation_id"],
        "message_id": payload["id"],
        "status": status,
        "reply_to": pending["confirmation_id"],
        "routing": "project_confirmation",
    }


def _notify(client, agent, path, pending):
    if pending.get("notified"):
        return
    if not pending.get("notice"):
        pending["notice"] = _status_notice(client, agent, pending)
        _write_json(path, pending)
    _append_once(client, pending["notice"])
    pending["notified"] = True
    _write_json(path, pending)


def _confirmation_result(pending):
    payload = pending.get("release") or pending["payload"]
    result = {
        "status": pending["status"],
        "id": payload["id"],
        "to": payload["to"],
        "confirmation_id": pending["confirmation_id"]}
    if pending.get("reason"):
        result["reason"] = pending["reason"]
    return result


def _claim_pending(client, agent, path, confirmation_id):
    """Load the held request and fix it to this window, or refuse it.

    Args:
        client (Bus): Connection whose private state holds the request.
        agent (str): CLI name of the window deciding.
        path (str): The pending file, already locked by the caller.
        confirmation_id (str): The id being decided, for the error text.

    Returns:
        dict: The pending record.

    Raises:
        ValueError: The id is unknown, or the request belongs to another
            window.
    """
    pending = _load_json(path)
    if pending is None:
        raise ValueError(f"unknown project confirmation {confirmation_id!r}")
    if not routing.addressed_to(client, agent, pending["probe"]):
        raise ValueError(
            "project confirmation is addressed to another window")
    # Unknown handles become a specific window on the first decision,
    # including no/expiry, so another claimant cannot change that result.
    if not pending["probe"].get("to_session"):
        pending["probe"].update(to_session=client.session, to_agent=agent)
        _write_json(path, pending)
    presence.touch(client, agent)
    return pending


@context_lock.locked
def confirm_project(client, agent, confirmation_id, accept):
    """Accept or reject a held message from the addressed window, exactly once.

    No/expiry never appends the payload. Yes pins formerly unknown handles
    to this actual session, then establishes a bidirectional relationship
    for the two current project contexts. Concurrent and repeated calls use
    a persisted decision under the request's exclusive lock.
    """
    state.check_name(agent)
    if not isinstance(accept, bool):
        raise ValueError("project confirmation requires an explicit yes or no")
    path = _pending_path(client, confirmation_id)
    with open(path + ".lock", "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        pending = _claim_pending(client, agent, path, confirmation_id)
        if pending["status"] in _FINAL_STATUSES:
            _notify(client, agent, path, pending)
            return _confirmation_result(pending)

        released = pending.get("release")
        already_released = released is not None and _already_appended(
            client, released["id"])
        source = _canonical_context(client, pending["source_context"])
        current_source = _current_context(
            client,
            source["session"],
            source["agent"],
            source.get("session_start"))
        recipient = _local_context(client, agent)
        if not already_released and time.time() >= pending["expires_at"]:
            pending.update(status="expired", reason="confirmation_timeout")
        elif not already_released and current_source != source:
            pending.update(status="expired", reason="sender_context_changed")
        elif (not already_released and not accept
              and pending.get("decision") is None):
            pending.update(status="rejected", decision=False)
            _relationship(client, source, recipient, revoke=True)
        else:
            if released is None:
                released = dict(pending["payload"])
                released.pop("broadcast", None)
                released.update(
                    ts=time.time(),
                    to=state.current_handle(
                        client,
                        agent),
                    to_session=client.session,
                    to_agent=agent,
                    from_session=source["session"],
                    confirmation_id=confirmation_id)
                pending.update(
                    status="releasing",
                    decision=True,
                    release=released,
                    recipient_context=recipient)
                _write_json(path, pending)
            if released["kind"] == "task" and not already_released:
                _task_status(client, released, "confirmed", confirmation_id)
            _append_once(client, released)
            if released["kind"] == "result":
                _task_status(client, released, "confirmed", confirmation_id)
            if pending["project"] == source["job"]:
                _relationship(
                    client,
                    source,
                    pending.get(
                        "recipient_context",
                        recipient),
                    confirmation_id=confirmation_id)
            pending["status"] = "confirmed"
        if pending["status"] in ("rejected", "expired"):
            _task_status(
                client,
                pending["payload"],
                pending["status"],
                confirmation_id)
        _write_json(path, pending)
        _notify(client, agent, path, pending)
        return _confirmation_result(pending)
