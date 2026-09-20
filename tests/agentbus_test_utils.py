"""Share process-identity fixtures across isolated bus regression tests."""

import contextlib
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock

import agentbus_identity as identity
import bus


@contextlib.contextmanager
def process_identity(pid=None, agent="claude", generation="1000"):
    """Model a CLI lifetime consistently across binding and routing tests.

    Args:
        pid (int or None): Window pid, or None to retain real ancestor lookup.
        agent (str): Command name exposed by the simulated CLI process.
        generation (str or None): Kernel start token for that process lifetime.

    Yields:
        None: Scope in which identity probes observe the simulated process.
    """
    with contextlib.ExitStack() as patches:
        patches.enter_context(mock.patch.object(
            identity, "_process_name", return_value=agent))
        patches.enter_context(mock.patch.object(
            identity, "_process_start", return_value=generation))
        if pid is not None:
            patches.enter_context(mock.patch.object(
                identity, "session_pid", return_value=pid))
        yield


@contextlib.contextmanager
def process_generation(generation):
    """Model process exit or pid reuse without changing the session fixture.

    Args:
        generation (str or None): Replacement kernel lifetime token.

    Yields:
        None: Scope observing the replacement process lifetime.
    """
    with mock.patch.object(
            identity, "_process_start", return_value=generation):
        yield


class BusFixture(unittest.TestCase):
    """Give a test its own bus directory and a way to open windows on it.

    Inherited from alongside IsolatedAsyncioTestCase by the asyncio
    suites, so the same setup serves both them and the synchronous ones
    without either inheriting the other's runner. It defines no tests of
    its own, so collecting it directly finds nothing to run.

    The bus lives in a temporary directory and AGENTBUS_JOB is cleared,
    so a test never reads the developer's real bus and never inherits a
    job from the shell that started it.

    Attributes:
        TEMP_PREFIX (str): Prefix for the temporary directory, so a
            leaked one names the suite that leaked it.
        DEFAULT_JOB (str): Job given to a window that does not ask for
            one.
    """

    TEMP_PREFIX = "agentbus-test-"
    DEFAULT_JOB = "project"

    def setUp(self):
        """Point this test at a bus of its own, cleaned up afterwards."""
        super().setUp()
        self.directory = tempfile.mkdtemp(prefix=self.TEMP_PREFIX)
        self.addCleanup(shutil.rmtree, self.directory, True)
        # The Stop-hook listen is a real wait on a real doorbell. It
        # belongs in a live window, not in a suite where it would add
        # its timeout to every case that ends a turn with no mail.
        environment = mock.patch.dict(
            os.environ, {"AGENTBUS_JOB": "", "AGENTBUS_STOP_WAIT": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def window(self, session, agent, handle, job=None):
        """Register one named window on this test's bus.

        Args:
            session (str): Session id, also used as the window's cwd so
                two windows do not derive the same job from the disk.
            agent (str): CLI name the window answers to.
            handle (str): Name to publish on the roster.
            job (str or None): Job to declare, or None for DEFAULT_JOB.

        Returns:
            Bus: Connection for that window.
        """
        client = bus.connect(self.directory, session=session,
                             cwd=os.path.join(self.directory, session))
        bus.set_job(client, job or self.DEFAULT_JOB)
        bus.register(client, agent)
        bus.set_name(client, handle)
        return client
