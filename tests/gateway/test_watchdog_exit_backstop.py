"""A fired watchdog must leave the process even when its own exit path hangs on disk I/O.

Under host I/O starvation (multi-second fsyncs) the liveness watchdog fired but its log write,
stack dumps and lifecycle stamps blocked, so the process took 5-14 minutes to exit and the
service supervisor could not restart it. The exit path is bounded by a backstop the hang cannot
starve.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import asyncio, threading
    import gateway.shutdown_watchdog as wd

    wd.DEFAULT_WATCHDOG_EXIT_BACKSTOP_S = 1.0
    # The dump write hangs the way a starved disk does; nothing after it ever runs.
    wd._write_watchdog_dump = lambda *a, **k: threading.Event().wait(600)

    async def main():
        loop = asyncio.get_running_loop()
        wd.start_loop_liveness_watchdog(loop, probe_interval=0.05, probe_timeout=0.05, max_strikes=1)
        threading.Event().wait(600)  # wedge the loop

    asyncio.run(main())
    """
)


def _run_child(home: Path, mode: str) -> tuple[int, float]:
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)}
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", _CHILD.replace("wd._write_watchdog_dump", mode)],
                          cwd=REPO_ROOT, env=env, capture_output=True, timeout=60, check=False)
    return proc.returncode, time.monotonic() - start


def test_liveness_watchdog_exits_when_its_dump_hangs(tmp_path):
    code, elapsed = _run_child(tmp_path, "wd._write_watchdog_dump")
    assert code != 0
    assert elapsed < 30, f"watchdog exit took {elapsed:.1f}s with a hung dump"


def test_liveness_watchdog_exits_when_lifecycle_stamp_hangs(tmp_path):
    code, elapsed = _run_child(tmp_path, "wd._mark_exited_quietly")
    assert code != 0
    assert elapsed < 30, f"watchdog exit took {elapsed:.1f}s with a hung lifecycle stamp"
