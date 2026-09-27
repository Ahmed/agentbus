"""Own isolated listener subprocesses so failed tests cannot leak waits."""

import contextlib as contexts
import subprocess as processes


@contexts.contextmanager
def child_process(command, **options):
    """End persistent test children before closing their standard streams.

    Args:
        command (list[str]): Test executable and arguments.
        **options (dict): Subprocess pipe and environment configuration.

    Yields:
        Popen: Child process available until the test context ends.
    """
    with processes.Popen(command, **options) as child:
        try:
            yield child
        finally:
            child.terminate()
            child.communicate(timeout=5)
