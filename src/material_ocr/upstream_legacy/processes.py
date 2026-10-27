"""Release an inference process, including its workers when it owns a session."""
import os
import signal
import subprocess


def stop_process(process, *, process_group=False, timeout=5):
    def send(sig):
        try:
            if process_group and os.name == 'posix':
                os.killpg(process.pid, sig)
            elif process.poll() is None:
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        process.wait(timeout=timeout)
    finally:
        # The parent may exit before a worker. A session is owned by this task.
        if process_group and os.name == 'posix':
            send(signal.SIGKILL)
