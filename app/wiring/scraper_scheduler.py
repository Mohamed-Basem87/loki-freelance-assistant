"""Container-resident scheduler for the LinkedIn/Wuzzuf/Upwork scraper.

The scraper (scraper/scraper.py) and its deps now live inside the image,
so no host cron / host-side scraper is needed: this worker runs the
scraper as a subprocess on the cadence the FileJobSource pollers read its
snapshot. Sources with the same configured poll_interval run in one
subprocess; different intervals run as independent loops under their own
worker liveness key (e.g. LinkedIn+Wuzzuf every 300s, Upwork every 60s).
The whole scraper pipeline stays inside the container.

Why a subprocess and not an import:

  * scraper/scraper.py drives scrapling/curl_cffi machinery
    and calls asyncio.run() itself. Running it as a separate process keeps
    its network/browser work and its event loop entirely out of the app's
    own loop and DBLogger thread, and a hung/rogue scrape kills only the
    child, not the app (the child is also hard-killed on timeout).
  * The app itself therefore never imports scrapling, so app imports work
    even in environments where only requirements.txt is installed.

The loop is a normal WorkerRegistry worker: it beats liveness during its
idle sleep (see app.infra.heartbeat.sleep_with_beats) and keeps looping across
transient scrape failures, so a broken scrape degrades gracefully instead
of tripping the TaskGroup's whole-process shutdown.
"""

import asyncio
import random
import sys
from pathlib import Path

from app.infra.heartbeat import sleep_with_beats, liveness, STATE_ALIVE, STATE_DEAD
from app.infra.runtime_config import BASE_DIR


WORKER_ID = "scraper_scheduler"

# Any adapter under this module is fed by the scraper snapshot; the
# scheduler groups those sources by poll interval and runs one subprocess
# per group. Kept here (not in healthcheck.py) so the worker ids the
# healthcheck requires are derived from the same grouping the registry
# actually starts.
SCRAPER_ADAPTER_PREFIX = "app.adapters.sources.scraper_file:"

# Jitter applied to a group's idle sleep so a 1-minute poll does not look
# like a metronome to the upstream site. Only short cadences are jittered;
# the 300s group keeps its exact cadence.
DEFAULT_JITTER_FRACTION = 0.15
DEFAULT_JITTER_MAX_INTERVAL = 120.0

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
# LinkedIn/Wuzzuf/Upwork data goes stale while the container stays HEALTHY.
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


async def _run_scraper_once(
    script_path,
    workdir,
    timeout=SCRAPER_TIMEOUT_SECONDS,
    args=(),
):
    """Run one scrape as a child process; return its return code.

    Captures stdout/stderr (merged) and prints the tail through the app's
    own log path so `docker logs` is the single observability surface.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        *args,
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


def _worker_id_for_interval(interval, multiple):
    """Stable liveness key for one schedule group.

    A single group keeps the historical ``scraper_scheduler`` id so
    existing healthcheck expectations/deployments do not change. With
    multiple cadences each group gets its own key, so one broken source
    group cannot keep the container "healthy" just because the other
    group is still beating.
    """
    if not multiple:
        return WORKER_ID
    label = int(interval) if float(interval).is_integer() else interval
    return f"{WORKER_ID}:{label}"


def scraper_schedule(job_sources):
    """Group scraper-file job sources into per-cadence runs.

    Returns a list of dicts: ``{"interval", "sources", "extra_args",
    "worker_id"}``. Sources sharing a poll interval share one subprocess
    invocation (``--sources a,b``); differing intervals become independent
    loops. ``extra_args`` are opaque CLI flags from the source's
    ``settings.scraper_args`` (e.g. the 1-minute Upwork fast-path page and
    detail-budget overrides).
    """
    groups = {}
    for cfg in job_sources or ():
        if not str(getattr(cfg, "adapter", "") or "").startswith(
            SCRAPER_ADAPTER_PREFIX
        ):
            continue
        interval = float(getattr(cfg, "poll_interval", 300) or 300)
        groups.setdefault(interval, []).append(cfg)

    multiple = len(groups) > 1
    schedule = []
    for interval in sorted(groups):
        cfgs = groups[interval]
        extra_args = []
        for cfg in cfgs:
            settings = getattr(cfg, "settings", None) or {}
            extra_args.extend(
                str(part)
                for part in (settings.get("scraper_args") or [])
            )
        schedule.append(
            {
                "interval": interval,
                "sources": [str(cfg.id) for cfg in cfgs],
                "extra_args": extra_args,
                "worker_id": _worker_id_for_interval(interval, multiple),
            }
        )
    return schedule


def scraper_scheduler_factory(
    script_path=None,
    workdir=None,
    interval=None,
    worker_id=WORKER_ID,
    sources=None,
    extra_args=None,
    schedules=None,
    jitter_fraction=DEFAULT_JITTER_FRACTION,
    jitter_max_interval=DEFAULT_JITTER_MAX_INTERVAL,
):
    """Build the scraper scheduler worker factory.

    Runs each schedule group once immediately (so a fresh snapshot exists
    as soon as the container starts, far ahead of the pollers' first
    tick), then every ``interval`` seconds. ``schedules`` is the
    multi-cadence form produced by :func:`scraper_schedule`; the older
    single-interval arguments remain supported for callers/tests.
    """
    script_path = Path(SCRAPER_SCRIPT if script_path is None else script_path)
    workdir = Path(SCRAPER_WORKDIR if workdir is None else workdir)

    if schedules is None:
        schedules = [
            {
                "interval": 300 if interval is None else interval,
                "sources": list(sources) if sources else [],
                "extra_args": list(extra_args or []),
                "worker_id": worker_id,
            }
        ]

    normalized = []
    for schedule in schedules:
        # Keep as a float: sleep_with_beats sleeps for exactly `interval`
        # and tests drive the loop with sub-second intervals. int()
        # truncated 0.05 to 0, turning the idle sleep into a busy-spin
        # that starved the rest of the event loop.
        normalized.append(
            {
                "interval": float(schedule.get("interval") or 300),
                "sources": list(schedule.get("sources") or []),
                "extra_args": list(schedule.get("extra_args") or []),
                "worker_id": schedule.get("worker_id") or worker_id,
            }
        )

    def _subprocess_args(schedule):
        args = []
        if schedule["sources"]:
            args.extend(
                ["--sources", ",".join(schedule["sources"])]
            )
        args.extend(schedule["extra_args"])
        return args

    async def _group_loop(schedule):
        group_worker_id = schedule["worker_id"]
        group_interval = schedule["interval"]
        label = ",".join(schedule["sources"]) or "all"
        args = _subprocess_args(schedule)
        consecutive_failures = 0

        while True:
            failed = False
            try:
                returncode = await _run_scraper_once(
                    script_path,
                    workdir,
                    args=args,
                )
                if returncode != 0:
                    failed = True
                    print(
                        f"[SCRAPER] {label} run exited with code "
                        f"{returncode}; treating as a failed run."
                    )
            except Exception as exc:  # keep the worker alive across failures
                failed = True
                print(
                    f"[SCRAPER] {label} run failed: "
                    f"{type(exc).__name__}: {exc}"
                )

            consecutive_failures = consecutive_failures + 1 if failed else 0

            if consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
                if consecutive_failures == CONSECUTIVE_FAILURE_THRESHOLD:
                    print(
                        f"[SCRAPER] {consecutive_failures} consecutive failed "
                        f"{label} runs; that data is now stale. Reporting "
                        f"worker {group_worker_id!r} as dead so the container "
                        "healthcheck stops reporting healthy while ingestion "
                        "is actually broken."
                    )
                loop_state = STATE_DEAD
            else:
                loop_state = STATE_ALIVE

            liveness.beat(group_worker_id, loop_state)

            delay = group_interval
            if (
                jitter_fraction
                and group_interval <= jitter_max_interval
            ):
                delay = group_interval * (
                    1.0 + random.uniform(
                        -float(jitter_fraction),
                        float(jitter_fraction),
                    )
                )
                delay = max(1.0, delay)

            await sleep_with_beats(
                delay,
                group_worker_id,
                state=loop_state,
            )

    async def _loop():
        if len(normalized) == 1:
            await _group_loop(normalized[0])
            return

        tasks = [
            asyncio.create_task(_group_loop(schedule))
            for schedule in normalized
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()

    return _loop
