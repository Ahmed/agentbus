"""Block on Linux bus-file events so idle windows need no timer scans."""

import ctypes as c_types
import os as system
import selectors as io_select
import struct as binary
import time as clock

# Linux UAPI inotify.h: close after writing, atomic replacement, and queue
# overflow. Watching directories also survives replacement of bus.jsonl.
_WRITE_EVENTS = 0x00000008 | 0x00000080
_OVERFLOW = 0x00004000
_EVENT_HEADER = binary.Struct("iIII")
# A read must hold at least one header plus NAME_MAX; 64 KiB batches bursts.
_READ_BYTES = 65536


class Listener:
    """Keep one kernel subscription alive across every message arrival."""

    def __init__(self, client, agent, pid=0):
        """Subscribe before the initial inbox read to close the startup race.

        Args:
            client (Bus): Bus directory and window identity to observe.
            agent (str): CLI whose presence changes can release deferred mail.
            pid (int): Optional owner process; its exit ends the listener.
        """
        self._selector = io_select.DefaultSelector()
        self._descriptors = []
        self._presence = f"presence.{agent}.{client.session}"
        self._library = c_types.CDLL(None, use_errno=True)
        self._library.inotify_init1.argtypes = [c_types.c_int]
        self._library.inotify_add_watch.argtypes = [
            c_types.c_int, c_types.c_char_p, c_types.c_uint32]
        self._fd = self._library.inotify_init1(
            system.O_NONBLOCK | system.O_CLOEXEC)
        if self._fd < 0:
            self.close()
            raise OSError(c_types.get_errno(), "cannot listen to bus events")
        self._descriptors.append(self._fd)
        try:
            self._bus_watch = self._watch(client.directory)
            self._state_watch = self._watch(client.state)
            self._selector.register(self._fd, io_select.EVENT_READ, "bus")
            if pid:
                self._library.pidfd_open.argtypes = [
                    c_types.c_int, c_types.c_uint]
                owner = self._library.pidfd_open(pid, 0)
                if owner < 0:
                    raise OSError(
                        c_types.get_errno(),
                        "cannot watch owner exit")
                self._descriptors.append(owner)
                self._selector.register(owner, io_select.EVENT_READ, "owner")
        except OSError:
            self.close()
            raise

    def _watch(self, path):
        """Observe the containing directory because writers replace files.

        Args:
            path (str): Existing directory containing bus or session state.

        Returns:
            int: Kernel watch descriptor used to identify events.
        """
        watch = self._library.inotify_add_watch(
            self._fd, system.fsencode(path), _WRITE_EVENTS)
        if watch < 0:
            raise OSError(c_types.get_errno(), "cannot watch bus directory")
        return watch

    def fileno(self):
        """Expose readiness for an async MCP transport without worker polling.

        Returns:
            int: Nonblocking inotify file descriptor.
        """
        return self._fd

    def drain(self):
        """Ignore unrelated state writes while retaining coalesced bus events.

        Returns:
            bool: Whether mail or relevant window identity/presence changed.
        """
        changed = False
        while True:
            try:
                data = system.read(self._fd, _READ_BYTES)
            except BlockingIOError:
                return changed
            position = 0
            while position < len(data):
                watch, mask, _cookie, length = _EVENT_HEADER.unpack_from(
                    data, position)
                position += _EVENT_HEADER.size
                name = system.fsdecode(
                    data[position:position + length]).rstrip("\0")
                position += length
                changed |= bool(mask & _OVERFLOW)
                changed |= watch == self._bus_watch and name == "bus.jsonl"
                changed |= watch == self._state_watch and (
                    name == self._presence or name.startswith("session."))

    def wait(self, timeout=None):
        """Sleep until an event or a deadline for mail already known to exist.

        Args:
            timeout (float or None): A pending delivery's grace period;
                None blocks indefinitely and is the empty-inbox default.

        Returns:
            bool: True on an event/deadline, False when the owner exits.
        """
        deadline = None
        if timeout is not None:
            deadline = clock.monotonic() + max(0, timeout)
        while True:
            remaining = None
            if deadline is not None:
                remaining = max(0, deadline - clock.monotonic())
            ready = self._selector.select(remaining)
            if not ready:
                return True
            if any(key.data == "owner" for key, _mask in ready):
                return False
            if self.drain():
                return True

    def close(self):
        """Release kernel watches even when startup or delivery fails.

        Returns:
            None: All owned descriptors are closed.
        """
        self._selector.close()
        for descriptor in self._descriptors:
            system.close(descriptor)
        self._descriptors.clear()

    def __enter__(self):
        """Tie the subscription lifetime to its shell or MCP process scope.

        Returns:
            Listener: This active subscription.
        """
        return self

    def __exit__(self, *_exception):
        """Ensure interrupted waits leave no open kernel watches.

        Args:
            *_exception (tuple): Context-manager exception information.

        Returns:
            None: Exceptions remain visible to the caller.
        """
        self.close()
