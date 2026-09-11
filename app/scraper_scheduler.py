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

from app.heartbeat import sleep_with_beats, liveness, STATE_ALIVE, STATE_DEAD
from app.runtime_config import BASE_DIR


WORKER_ID = "scraper_scheduler"

# How many consecutive failed runs (non-zero exit, or a hang killed by
# the timeout guard) before this worker reports itself "dead" instead of
# "alive". A single transient failure is a normal, self-healing event --
# the loop already keeps running and retries next cycle -- so it must
# stay "alive" (worker liveness is about the loop still turning, not
# about the last run's outcome). But nothing before this fix ever
# surfaced *sustained* failure anywhere: the scheduler swallows every
# exception and loops forever, so a scraper that has been broken for
# hours (site markup changed, every parse raises) looked identical to a
# healthy one to both the heartbeat and the container healthcheck --
# LinkedIn/Wuzzuf data goes stale while the container stays HEALTHY.
# Crossing this threshold makes that failure visible: healthcheck.py
# already fails the container when any critical worker reports "dead",
# so escalating here, without any change to healthcheck.py's core
# state-checking logic, turns "ingestion has been silently broken for a
# while" into an actual UNHEALTHY container. It self-heals the moment a
# run succeeds again -- no restart required.
CONSECUTIVE_FAILURE_THRESHOLD = 3

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
        consecutive_failures = 0
        while True:
            failed = False
            try:
                returncode = await _run_scraper_once(script_path, workdir)
                if returncode != 0:
                    failed = True
                    print(
                        f"[SCRAPER] run exited with code {returncode}; "
                        "treating as a failed run."
                    )
            except Exception as exc:  # keep the worker alive across failures
                failed = True
                print(f"[SCRAPER] run failed: {type(exc).__name__}: {exc}")

            consecutive_failures = consecutive_failures + 1 if failed else 0

            if consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
                if consecutive_failures == CONSECUTIVE_FAILURE_THRESHOLD:
                    print(
                        f"[SCRAPER] {consecutive_failures} consecutive failed "
                        "runs; LinkedIn/Wuzzuf data is now stale. Reporting "
                        f"worker {worker_id!r} as dead so the container "
                        "healthcheck stops reporting healthy while ingestion "
                        "is actually broken."
                    )
                loop_state = STATE_DEAD
            else:
                loop_state = STATE_ALIVE

            liveness.beat(worker_id, loop_state)
            await sleep_with_beats(interval, worker_id, state=loop_state)

    return _loop