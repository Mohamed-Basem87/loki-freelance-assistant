# URL Shortener Integration — Session Change Log

**Date:** 2026-09-11
**Author of this log:** Claude Code, on behalf of karim2004ashraf@gmail.com

## 1. What was requested

Unify every project's URL across all sources (Telegram, FreeHub, scraper
files) by routing it through an internal URL-shortener service **before**
the job is ever saved to the database. The shortened URL replaces the
original everywhere downstream: DB storage, dedup, and notifications.

Requirements gathered over the course of this session, in the order they
were pinned down:

1. Domain and endpoint live in two **separate** env variables.
2. Flow: `POST {domain}{endpoint}` with the original URL in the body →
   response carries the shortened URL → the shortened URL is what gets
   saved and sent, never the original (on the success path).
3. Request/response body shape: `{"url": "..."}` both ways.
4. Failure handling (transport error / timeout / 5xx / malformed body)
   must be **switchable via an env variable**, implemented as two
   separate handler functions/strategies on the service, not an
   if/else sprinkled through the call site.
5. No backfill — only new jobs go through the shortener; existing rows
   keep their original long URL.
6. **Duplicate detection**: the shortener can also signal "this exact
   URL was already shortened before" via a distinct **HTTP status
   code** (configurable, default `409`). That case is NOT a failure —
   it must stop the job the same way the existing job-existence checks
   already do (no DB row, no notification), and it is **not** subject
   to the failure-mode switch.
7. Confirmed: the existing "does this job already exist" check is keyed
   on `job_uuid` (derived from `identity_source` + `job_id`), never on
   the URL — so this change does not touch that dedup logic at all.

## 2. Final behavior implemented

```
process_job()
  │
  ├─ existing-row / job_uuid dedup checks           (unchanged, URL not involved)
  ├─ deterministic classification                    (unchanged)
  │
  ├─ [only for genuinely new jobs, right before the row is created]
  │     shortened = await url_shortener.shorten(job["url"])
  │
  │     ├─ 200 OK  {"url": "<short>"}
  │     │     → job["url"] = short url; continue to create_job_if_absent
  │     │       (DB row is created with the SHORT url — the long url
  │     │        never touches the jobs table on this path)
  │     │
  │     ├─ 409 (URL_SHORTENER_DUPLICATE_STATUS)
  │     │     → UrlAlreadyExistsError, caught locally in process_job():
  │     │       print + return. No DB row. No notification. Source still
  │     │       marks the message/project "seen" (this is a normal
  │     │       return, not an exception) so it is never retried.
  │     │       Always happens, regardless of URL_SHORTENER_FAILURE_MODE.
  │     │
  │     └─ any other failure (timeout / other non-2xx / bad body)
  │           → dispatched by URL_SHORTENER_FAILURE_MODE:
  │              fail_open   (default): log + keep the ORIGINAL url,
  │                          continue the pipeline normally.
  │              fail_closed: raise UrlShorteningError, uncaught, out of
  │                          process_job(). No DB row created. The
  │                          calling source (FreeHub's SourceWorker, or
  │                          Telegram's process_message()) does not mark
  │                          the item "seen" / does not advance its
  │                          watermark, so the source naturally retries
  │                          the same job on its next poll. No new
  │                          durable "pending" DB state was needed for
  │                          this — it reuses the seen/watermark
  │                          mechanism every other process_job() failure
  │                          already relies on.
  │
  ▼
create_job_if_absent(... url=job["url"] ...)   ← always the resolved URL
  │
  ▼
... rest of pipeline unchanged (classification, notification, etc.)
```

Cross-source project-id extraction (`_extract_project_id`, used for the
existing FreeHub/Telegram duplicate-project dedup) reads `job["url"]`
**before** the shortening step runs, so it always sees the original URL
and is unaffected by this change.

## 3. Predicted shortener responses this code expects

| Scenario | HTTP status | Body | Code behavior |
|---|---|---|---|
| New URL, shortened successfully | `200` | `{"url": "http://short.example/abc123"}` | Stores/sends the shortened URL |
| URL already shortened before | `URL_SHORTENER_DUPLICATE_STATUS` (default `409`) | irrelevant, not read | Job skipped entirely, no row created |
| Transport/DNS/connection failure or timeout | — (exception, no status) | — | `fail_open`→ keep original URL; `fail_closed` → raise, job retried later |
| Any other non-2xx (e.g. 500, 503) | not `200`, not the duplicate status | irrelevant | Same as above (`fail_open`/`fail_closed`) |
| 200 but malformed/missing `"url"` field | `200` | e.g. `{}` or `{"foo": "bar"}` | Treated as a failure — same as above (`fail_open`/`fail_closed`) |

If the real service's duplicate signal ever turns out to use a
different status code, only `URL_SHORTENER_DUPLICATE_STATUS` in `.env`
needs to change — no code edit required.

## 4. Files changed

| File | Change |
|---|---|
| `.env.example` | Added `URL_SHORTENER_DOMAIN`, `URL_SHORTENER_ENDPOINT`, `URL_SHORTENER_FAILURE_MODE` (default `fail_open`), `URL_SHORTENER_DUPLICATE_STATUS` (default `409`). |
| `app/runtime_config.py` | Added 4 fields to `RuntimePolicy` and to the `RUNTIME` construction. Only `failure_mode`/`duplicate_status` are validated eagerly at import time (they always have safe defaults); `domain`/`endpoint` are deliberately left unvalidated here — see the inline comment and item 5 below. |
| `app/url_shortener.py` | **New file.** `UrlShortenerService` (the `shorten()` call, request/response contract, `fail_open`/`fail_closed` dispatched via a dict of two separate methods), `UrlAlreadyExistsError`, `UrlShorteningError`. |
| `app/dependencies.py` | New late-bound proxy slot `url_shortener` (`allowed={"shorten"}`), and a new `url_shortener_service` parameter on `configure()`, following the exact same pattern as `notifier`/`router`/`resolver`. |
| `app/composition.py` | Builds `UrlShortenerService` using the already-shared `http_transport` (no second connection pool), validates `URL_SHORTENER_DOMAIN`/`ENDPOINT` are actually set (lazily, at composition time — see item 5), and binds it via `configure(url_shortener_service=...)`. |
| `app/job_processor.py` | The actual pipeline change: inside `process_job()`, inside the existing `if not existing_incomplete:` block, right before `create_job_if_absent(...)`, calls `url_shortener.shorten(job["url"])`, replaces `job["url"]` with the result, and catches `UrlAlreadyExistsError` locally to skip the job. |
| `tests/conftest.py` | Added `URL_SHORTENER_DOMAIN`/`ENDPOINT` test placeholders to `_TEST_ENV_DEFAULTS` (same convention as the other required vars), and bound a pass-through `_NoopUrlShortener` in the shared dependency-binding fixture so every pre-existing test keeps working unchanged. |
| `tests/test_url_shortener.py` | **New file.** Unit tests for `UrlShortenerService`: success, URL/slash normalization, duplicate-status (both failure modes), fail_open fallback, fail_closed raise, timeout-style exception with no `.status` attribute, malformed response body, invalid `failure_mode` rejected at construction. |
| `tests/test_pipeline.py` | 3 new integration tests: URL is shortened before the row is created (and the shortener receives the *original* URL); a duplicate signal skips job creation without raising; a fail_closed failure leaves no row and is reported as a failed message (not a crash) through `process_message()`. |
| `DOCUMENTATION.md` | New "6a. URL Shortening" section describing where it runs, the request/response contract, duplicate handling, failure handling, and scope (new jobs only, no backfill). |
| `docs/url-shortener-changes.md` | **New file.** This document. |

## 5. Design decisions worth flagging

- **`domain`/`endpoint` validation is lazy (composition-time), not
  eager (import-time).** `app/runtime_config.py` is imported
  transitively by a large part of the app and test suite before any
  test-environment defaults are applied (see `tests/conftest.py`'s own
  docstring on this exact issue for the pre-existing required vars in
  `app/config.py`). Making these two fields hard-required at
  `runtime_config` import time broke that ordering and failed test
  collection; the fix follows the project's existing pattern
  (`get_api_id()`, `get_freehub_user_id()`, etc.) of raising only when
  the composition root actually needs the value, in
  `app.composition.compose()`.
- **The duplicate signal is a plain HTTP status check, not a JSON
  field**, per your answer. It's read off the exception the transport
  raises on non-2xx (`aiohttp.ClientResponseError.status`), not off a
  successful response body.
- **`UrlAlreadyExistsError` is caught inside `process_job()` itself**,
  not left to propagate — this mirrors the two existing duplicate-skip
  code paths already in that function (legacy-identity match,
  cross-source project-id claim loss): a normal `return`, no DB row, no
  exception surfaced to the caller, so the source marks the item seen
  and never retries a genuine duplicate.
- **`UrlShorteningError` (fail_closed) is deliberately left
  uncaught** inside `process_job()`. Both call sites already had an
  established "don't mark seen / don't advance watermark on any
  exception" behavior (`app/source_worker.py` for FreeHub,
  `app/message_processor.py` for Telegram) — reusing it avoided adding
  a new durable "pending" DB state solely for this feature.

## 6. Test results

```
python -m pytest tests/ -q --ignore=tests/test_llm_gemini.py --ignore=tests/test_llm_groq.py --ignore=tests/test_wuzzuf_extraction.py
610 passed, 2 warnings (pre-existing, unrelated Windows/asyncio proactor
cleanup noise in tests/test_state.py)
```

`test_llm_gemini.py`/`test_llm_groq.py` are opt-in live-credential
tests (skipped, as they require real API keys). `test_wuzzuf_extraction.py`
was skipped because it depends on the `scrapling` package, which was not
installed in this environment (unrelated to this change — it belongs to
the scraper subsystem, not the pipeline touched here).

## 7. Not done / explicitly out of scope

- No backfill/migration of existing job rows' URLs (per your answer).
- `app/config.py` (the legacy compatibility config surface) was left
  untouched — the newer `runtime_config.py` surface already covers
  this feature; let me know if you also want compatibility constants
  there.
- The real shortener service's actual behavior has not been verified
  against a live endpoint — everything above matches the contract you
  described, but you'll want a smoke test against the real service
  once `URL_SHORTENER_DOMAIN`/`ENDPOINT` are set to real values.
