"""URL shortener service.

Every project URL is run through an internal shortener before the job
row is ever created (see app.services.job_processor.process_job). This module
owns the semantic contract for that call; the concrete HTTP mechanics
stay behind the injected HttpTransport port (see app.ports.HttpTransport
/ app.adapters.http.aiohttp.AioHttpTransport), exactly like FreeHub's
client.

Contract with the upstream shortener service:
    POST {domain}{endpoint}
    body:     {"jobId": "<source's own job id>", "job_link": "<original project url>"}
    success:  200/201 {"path": "/<jobId>"} -- the shortener is an
              idempotent upsert keyed by jobId: if this jobId was already
              shortened before, the existing record's path is returned
              instead of a conflict error. `jobId` is deliberately the
              same job id the rest of the pipeline already uses (see
              app.services.job_processor.process_job's `job_id` parameter), so
              this call never needs to check for prior existence itself
              -- the shortener does that check, keyed on the id we
              already have.
    any other non-2xx / timeout / malformed body: a genuine failure,
              handled per FAILURE_MODE (see below).

FAILURE_MODE (env: URL_SHORTENER_FAILURE_MODE) toggles what a genuine
failure does, without any code change:
    fail_open   -- log and fall back to the original (long) URL; the
                   job pipeline continues normally. Default.
    fail_closed -- raise UrlShorteningError. process_job() does not
                   catch this, so it propagates out to the calling
                   source worker (see app.wiring.source_worker.SourceWorker.run),
                   which logs it and does NOT call mark_seen()/advance
                   the watermark -- the job is therefore retried from
                   scratch on the source's next poll, with no new durable
                   "pending" state needed here.
"""
import logging

logger = logging.getLogger(__name__)


class UrlShorteningError(RuntimeError):
    """A genuine shortener failure (timeout, non-2xx, or a malformed
    response body).

    Only ever raised/returned-from by the fail_closed strategy; the
    fail_open strategy swallows the same failures and falls back to the
    original URL instead of raising this.
    """


class UrlShortenerService:
    """Runs one project URL through the configured shortener endpoint.

    `failure_mode` selects between two independent handler methods via
    a plain dict dispatch built once at construction time, so behavior
    is switched entirely by the URL_SHORTENER_FAILURE_MODE env var (see
    app.core.runtime_config) -- no code change needed to flip it.
    """

    def __init__(self, transport, domain: str, endpoint: str,
                 failure_mode: str = "fail_open"):
        if failure_mode not in ("fail_open", "fail_closed"):
            raise ValueError(f"Unknown url shortener failure_mode: {failure_mode!r}")
        self._transport = transport
        self._domain = domain.rstrip("/")
        self._url = self._domain + "/" + endpoint.lstrip("/")
        self._failure_mode = failure_mode
        self._on_failure = {
            "fail_open": self._fail_open,
            "fail_closed": self._fail_closed,
        }[failure_mode]

    async def shorten(self, job_id: str, original_url: str) -> str:
        """Return the shortened URL for `original_url`, keyed by `job_id`.

        `job_id` must be the same source job id the rest of the pipeline
        already uses to identify this job -- reusing it here is what
        lets the shortener's upsert-by-jobId behavior stand in for a
        separate existence check.

        On failure, returns per failure_mode:
            fail_open   -> original_url (never raises)
            fail_closed -> raises UrlShorteningError
        """
        try:
            response = await self._transport.request(
                "POST", self._url, json={"jobId": job_id, "job_link": original_url}
            )
        except Exception as exc:
            return await self._on_failure(original_url, exc)

        try:
            data = await response.json()
        except Exception as exc:
            return await self._on_failure(
                original_url,
                UrlShorteningError(f"Malformed shortener response: {exc}"),
            )

        path = str((data or {}).get("path") or "").strip()
        if not path:
            return await self._on_failure(
                original_url,
                UrlShorteningError("Shortener response missing 'path' field"),
            )
        return self._domain + "/" + path.lstrip("/")

    async def _fail_open(self, original_url: str, exc: Exception) -> str:
        logger.warning(
            "URL shortener request failed (fail_open: keeping original URL) "
            "for %r: %s",
            original_url,
            exc,
        )
        return original_url

    async def _fail_closed(self, original_url: str, exc: Exception) -> str:
        logger.error(
            "URL shortener request failed (fail_closed: job will be "
            "retried by its source) for %r: %s",
            original_url,
            exc,
        )
        raise UrlShorteningError(str(exc)) from exc
