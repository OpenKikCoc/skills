#!/usr/bin/env python3
"""Supervise the complete local probe, including classification and output."""

import math
import os
import select
import signal
import subprocess
import sys
import tempfile
import time


def write_bounded(fd, data, deadline):
    os.set_blocking(fd, False)
    while data:
        try:
            count = os.write(fd, data)
            data = data[count:]
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                return False
        except OSError:
            return False
    return True


def main():
    try:
        timeout = float(sys.argv[1])
        if not math.isfinite(timeout) or timeout <= 0 or len(sys.argv) < 4:
            raise ValueError
    except (IndexError, ValueError):
        write_bounded(2, b"error: --timeout-seconds must be finite and positive\n", time.monotonic())
        return 2
    selectors = sys.argv[2]
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="probe-observer.") as temporary:
        os.chmod(temporary, 0o700)
        env = dict(os.environ, PROBE_DEADLINE_WORKER=str(os.getpid()), TMPDIR=temporary)
        with tempfile.TemporaryFile(dir=temporary) as output, tempfile.TemporaryFile(dir=temporary) as errors:
            process = None
            try:
                process = subprocess.Popen(sys.argv[3:], stdout=output, stderr=errors,
                                           env=env, start_new_session=True)
                rc = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                output.seek(0)
                errors.seek(0)
                # Output is file-backed until the complete capture has been validated.
                data = output.read(1048577)
                error = errors.read(4096)
                if len(data) > 1048576 or time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                if not write_bounded(1, data, deadline):
                    raise subprocess.TimeoutExpired(process.args, timeout)
                if not write_bounded(2, error, deadline):
                    raise subprocess.TimeoutExpired(process.args, timeout)
                return rc
            except subprocess.TimeoutExpired:
                if process is not None:
                    # This group contains only the local probe and its observer children.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=0.03)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                now = time.monotonic()
                write_bounded(1, "".join(f"{g}|probe_error|-|||\n" for g in selectors.split(",")).encode(), now)
                write_bounded(2, b"error: observer/transport timeout; remote work is unchanged\n", now)
                return 3
            except OSError:
                write_bounded(2, b"error: local observer executable unavailable\n", time.monotonic())
                return 3


if __name__ == "__main__":
    raise SystemExit(main())
