"""URL shortener service.

Every project URL is run through an internal shortener before the job
row is ever created (see app.job_processor.process_job). This module
owns the semantic contract for that call; the concrete HTTP mechanics
stay behind the injected HttpTransport port (see app.ports.HttpTransport
/ app.adapters.http.aiohttp.AioHttpTransport), exactly like FreeHub's
client.

Contract with the upstream shortener service:
    POST {domain}{endpoint}
    body:     {"url": "<original project url>"}
    success:  200 {"url": "<shortened url>"}
    duplicate: a configured HTTP status (URL_SHORTENER_DUPLICATE_STATUS,
               default 409) meaning this exact URL was already shortened
               before -- this is NOT a failure, it is an authoritative
               duplicate signal and is never subject to FAILURE_MODE.
    any other non-2xx / timeout / malformed body: a genuine failure,
               handled per FAILURE_MODE (see below).

FAILURE_MODE (env: URL_SHORTENER_FAILURE_MODE) toggles what a genuine
failure does, without any code change:
    fail_open   -- log and fall back to the original (long) URL; the
                   job pipeline continues normally. Default.
    fail_closed -- raise UrlShorteningError. process_job() does not
                   catch this, so it propagates out to the calling
                   source worker (see app.source_worker.SourceWorker.run),
                   which logs it and does NOT call mark_seen()/advance
                   the watermark -- the job is therefore retried from
                   scratch on the source's next poll, with no new durable
                   "pending" state needed here.
"""
import logging

logger = logging.getLogger(__name__)


class UrlShorteningError(RuntimeError):
    """A genuine shortener failure (timeout, non-2xx other than the
    configured duplicate status, or a malformed response body).

    Only ever raised/returned-from by the fail_closed strategy; the
    fail_open strategy swallows the same failures and falls back to the
    original URL instead of raising this.
    """


class UrlAlreadyExistsError(RuntimeError):
    """The shortener reported (via URL_SHORTENER_DUPLICATE_STATUS) that
    this exact original URL was already shortened before.

    This is an authoritative duplicate signal, not a failure -- it is
    always raised regardless of FAILURE_MODE, and app.job_processor
    catches it specifically to skip the job the same way it already
    skips other duplicate-detection paths (legacy identity match,
    cross-source project-id claim failure): no DB row is created, no
    notification is sent, and the source marks the job seen as usual so
    it is not retried forever.
    """

    def __init__(self, original_url: str):
        super().__init__(f"URL already shortened previously: {original_url!r}")
        self.original_url = original_url


class UrlShortenerService:
    """Runs one project URL through the configured shortener endpoint.

    `failure_mode` selects between two independent handler methods via
    a plain dict dispatch built once at construction time, so behavior
    is switched entirely by the URL_SHORTENER_FAILURE_MODE env var (see
    app.runtime_config) -- no code change needed to flip it.
    """

    def __init__(self, transport, domain: str, endpoint: str,
                 failure_mode: str = "fail_open",
                 duplicate_status: int = 409):
        if failure_mode not in ("fail_open", "fail_closed"):
            raise ValueError(f"Unknown url shortener failure_mode: {failure_mode!r}")
        self._transport = transport
        self._url = domain.rstrip("/") + "/" + endpoint.lstrip("/")
        self._failure_mode = failure_mode
        self._duplicate_status = duplicate_status
        self._on_failure = {
            "fail_open": self._fail_open,
            "fail_closed": self._fail_closed,
        }[failure_mode]

    async def shorten(self, original_url: str) -> str:
        """Return the shortened URL for `original_url`.

        Raises UrlAlreadyExistsError if the shortener reports this URL
        was already shortened before (see module docstring) --
        unconditional, regardless of failure_mode.

        On any other failure, returns per failure_mode:
            fail_open   -> original_url (never raises)
            fail_closed -> raises UrlShorteningError
        """
        try:
            response = await self._transport.request(
                "POST", self._url, json={"url": original_url}
            )
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == self._duplicate_status:
                raise UrlAlreadyExistsError(original_url) from exc
            return await self._on_failure(original_url, exc)

        try:
            data = await response.json()
        except Exception as exc:
            return await self._on_failure(
                original_url,
                UrlShorteningError(f"Malformed shortener response: {exc}"),
            )

        shortened = str((data or {}).get("url") or "").strip()
        if not shortened:
            return await self._on_failure(
                original_url,
                UrlShorteningError("Shortener response missing 'url' field"),
            )
        return shortened

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
