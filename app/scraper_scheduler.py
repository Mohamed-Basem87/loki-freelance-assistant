"""Container-resident scheduler for the LinkedIn/Wuzzuf scraper.

The scraper (scraper/scraper.py) and its deps now live inside the image,
so no host cron / host-side scraper is needed: this worker runs the
scraper as a subprocess on the same cadence the FileJobSource pollers read
its snapshot (config job-source poll_interval; 300s for both linkedin and
wuzzuf). The whole LinkedIn/Wuzzuf pipeline stays inside the container.

Why a subprocess and not an import:

  * scraper/scraper.py drives scrapling/curl_cffi/playwright machinery
    and calls asyncio.run() itself. Running it as a separate process keeps
    its network/browser work and its event loop entirely out of the app's
    own loop and DBLogger thread, and a hung/rogue scrape kills only the
    child, not the app (the child is also hard-killed on timeout).
  * The app itself therefore never imports scrapling, so app imports work
    even in environments where only requirements.txt is installed.

The loop is a normal WorkerRegistry worker: it beats liveness during its
idle sleep (see app.heartbeat.sleep_with_beats) and keeps looping across
transient scrape failures, so a broken scrape degrades gracefully instead
of tripping the TaskGroup's whole-process shutdown.
"""

import asyncio
import sys
from pathlib import Path

from app.heartbeat import sleep_with_beats, STATE_ALIVE
from app.runtime_config import BASE_DIR


WORKER_ID = "scraper_scheduler"

# The scraper writes OUTPUT_FILE (`jobs_results.json`) and its
# seen-state sidecar to CWD, and the FileJobSource config points at
# scraper/jobs_results.json relative to BASE_DIR -- so the subprocess must
# run with BASE_DIR/scraper as its working directory.
SCRAPER_SCRIPT = Path(BASE_DIR) / "scraper" / "scraper.py"
SCRAPER_WORKDIR = Path(BASE_DIR) / "scraper"

# A single scrape takes ~20s on prod; the timeout is a hang guard, not a
# budget. Excision of a stuck child is the point (see module docstring).
SCRAPER_TIMEOUT_SECONDS = 600

# How much of the child's stdout to echo into the app log per run.
TAIL_LINES = 15


async def _run_scraper_once(script_path, workdir, timeout=SCRAPER_TIMEOUT_SECONDS):
    """Run one scrape as a child process; return its return code.

    Captures stdout/stderr (merged) and prints the tail through the app's
    own log path so `docker logs` is the single observability surface.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output_bytes, _ = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise TimeoutError(
            f"scraper did not finish within {timeout}s and was killed"
        )

    text = (output_bytes or b"").decode(errors="replace")
    lines = text.rstrip().splitlines()
    print(f"[SCRAPER] finished run exit={process.returncode}")
    for line in lines[-TAIL_LINES:]:
        print(f"[SCRAPER] {line}")
    return process.returncode


def scraper_scheduler_factory(
    script_path=None,
    workdir=None,
    interval=None,
    worker_id=WORKER_ID,
):
    """Build the `scraper_scheduler` worker factory.

    Runs once immediately (so a fresh snapshot exists as soon as the
    container starts, far ahead of the pollers' first tick), then every
    ``interval`` seconds. ``interval`` defaults to the FileJobSource poll
    interval (300s) so scheduling and polling stay in lockstep.
    """
    script_path = Path(SCRAPER_SCRIPT if script_path is None else script_path)
    workdir = Path(SCRAPER_WORKDIR if workdir is None else workdir)
    interval = 300 if interval is None else int(interval)

    async def _loop():
        while True:
            try:
                await _run_scraper_once(script_path, workdir)
            except Exception as exc:  # keep the worker alive across failures
                print(f"[SCRAPER] run failed: {type(exc).__name__}: {exc}")
            await sleep_with_beats(interval, worker_id, state=STATE_ALIVE)

    return _loop