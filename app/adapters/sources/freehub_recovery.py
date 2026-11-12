"""Recover FreeHub project descriptions that upstream corrupts.

The FreeHub API occasionally puts the client's name, the project price,
the title, or an empty string into a Kafiil project's ``description``
field.  The FreeHub detail endpoint sometimes has the real text; the
public Kafiil project page is the final source of truth.  This module
detects those placeholder descriptions and repairs them at ingestion
time, before the job is persisted.

Only descriptions that look suspicious (empty, title/price, or very
short) trigger extra requests.  Everything else passes through
untouched, so normal ingestion keeps its current cost.
"""

import asyncio
import html
import re
from urllib.parse import urlparse

# Placeholder descriptions observed upstream are client names or budget
# strings -- all short.  Real Kafiil briefs are almost always longer.
_SUSPICIOUS_MAX_CHARS = 60

# If the detail endpoint gives a real but very short description, also
# consult the project page in case the detail payload is an excerpt.
_SHORT_REAL_DESCRIPTION_CHARS = 60

_KAFIIL_HOST = "kafiil.com"
_DESCRIPTION_MARKER_RE = re.compile(r"تفاصيل\s+المشروع")
_DESCRIPTION_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_SPACES_RE = re.compile(r"[ \t]+")
_NEWLINES_RE = re.compile(r"\n{3,}")

_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.8",
}

_CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "SAR": "ر.س",
    "AED": "د.إ",
    "EGP": "ج.م",
}


def clean_text(text):
    """Normalize a raw API/HTML description without truncating it."""
    if not text:
        return ""
    cleaned = html.unescape(str(text))
    cleaned = cleaned.replace("\u200b", "").replace("\ufeff", "")
    cleaned = cleaned.replace("\xa0", " ")
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _SPACES_RE.sub(" ", cleaned)
    cleaned = _NEWLINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def collapse(text):
    """Collapse every whitespace run, for reliable equality checks."""
    return _WHITESPACE_RE.sub(" ", clean_text(text)).strip()


def _comparable(text):
    return collapse(text).casefold()


def looks_suspicious(project):
    """True when a list payload's description is probably a placeholder.

    Short descriptions are included deliberately: that is how a bare
    client name or budget slips through.  The detail/page fallbacks only
    replace the text when they produce a better, non-placeholder value,
    so a legitimately short description is left as-is.
    """
    description = collapse(project.get("description"))
    if not description:
        return True
    if description == collapse(project.get("title")):
        return True
    price = collapse(project.get("price"))
    if price and description == price:
        return True
    return len(description) <= _SUSPICIOUS_MAX_CHARS


def extract_kafiil_description(page_html):
    """Extract the "تفاصيل المشروع" paragraph from a Kafiil project page."""
    if not page_html:
        return ""
    marker = _DESCRIPTION_MARKER_RE.search(page_html)
    if marker is None:
        return ""
    # The description paragraph immediately follows the block heading.
    tail = page_html[marker.end(): marker.end() + 200000]
    match = _DESCRIPTION_P_RE.search(tail)
    if match is None:
        return ""
    text = _BR_RE.sub("\n", match.group(1))
    text = _TAG_RE.sub(" ", text)
    return clean_text(text)


def _budget_values(budget):
    """Strings a budget often appears as inside a description field."""
    if not isinstance(budget, dict):
        return []
    minimum = budget.get("min")
    maximum = budget.get("max")
    currency = budget.get("currency")
    symbol = _CURRENCY_SYMBOLS.get(str(currency or "").upper())
    values = []
    for amount in (minimum, maximum):
        if amount is None:
            continue
        values.append(str(amount))
        if symbol:
            values.append(f"{symbol}{amount}")
    if minimum is not None and maximum is not None:
        values.append(f"{minimum} - {maximum}")
        values.append(f"{minimum}-{maximum}")
        if currency:
            values.append(f"{currency} {minimum} - {currency} {maximum}")
        if symbol:
            values.append(f"{symbol}{minimum} - {symbol}{maximum}")
            values.append(f"{symbol}{minimum}-{symbol}{maximum}")
    return values


def _client_name(detail):
    if not isinstance(detail, dict):
        return ""
    client = detail.get("client")
    if not isinstance(client, dict):
        return ""
    return client.get("name") or ""


def _placeholder_values(project, detail):
    """Every value that must never be accepted as the real description."""
    values = [project.get("title"), project.get("price")]
    if isinstance(detail, dict):
        values.append(detail.get("title"))
        values.append(_client_name(detail))
        values.extend(_budget_values(detail.get("budget")))
    return {
        _comparable(value)
        for value in values
        if value is not None and collapse(value)
    }


def _is_usable(text, placeholders):
    cleaned = clean_text(text)
    if not cleaned:
        return False
    return _comparable(cleaned) not in placeholders


def _is_kafiil_url(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == _KAFIIL_HOST or host.endswith("." + _KAFIIL_HOST)


class FreeHubDescriptionRecovery:
    """Repair placeholder FreeHub descriptions before they are persisted.

    ``client`` is a FreeHubApiClient (or any object exposing
    ``fetch_project(uid)`` and ``fetch_text(url, headers=...)``), which
    reuses the source's existing HTTP transport/connection pool.
    """

    def __init__(self, client, *, sources=("kafiil",), max_concurrency=4):
        self._client = client
        self._sources = {str(source).lower() for source in sources}
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def _applies_to(self, project):
        source = project.get("_poll_source") or project.get("platform") or ""
        return str(source).lower() in self._sources

    async def enrich_all(self, projects):
        """Repair any suspicious projects in place and return the same list."""
        targets = [
            project
            for project in projects
            if self._applies_to(project) and looks_suspicious(project)
        ]
        if not targets:
            return projects
        await asyncio.gather(
            *(self._enrich_one(project) for project in targets),
            return_exceptions=True,
        )
        return projects

    async def _enrich_one(self, project):
        uid = str(project.get("uid") or "")
        if not uid:
            return
        async with self._semaphore:
            try:
                await self._recover(project, uid)
            except Exception as exc:
                # Recovery is best-effort: never fail ingestion because a
                # fallback source was unavailable or the page changed.
                print(f"[FREEHUB RECOVERY] {uid}: {type(exc).__name__}: {exc}")

    async def _recover(self, project, uid):
        detail = await self._fetch_detail(uid)
        placeholders = _placeholder_values(project, detail)

        original = clean_text(project.get("description"))
        original_comparable = _comparable(original)
        client_name = _client_name(detail)
        # A missing/short text is only treated as a placeholder.  If the
        # original is a legitimately short brief, the fallbacks below must
        # not make it worse.
        original_is_placeholder = (
            not original_comparable
            or original_comparable == _comparable(project.get("title"))
            or original_comparable == _comparable(project.get("price"))
            or (
                client_name
                and original_comparable == _comparable(client_name)
            )
        )

        detail_description = clean_text(
            detail.get("description") if isinstance(detail, dict) else ""
        )
        detail_usable = _is_usable(detail_description, placeholders)
        # A detail payload identical to the suspicious list text is not an
        # improvement (and may be the same client name).  Consult the page.
        if detail_usable and _comparable(detail_description) == original_comparable:
            detail_usable = False

        best = detail_description if detail_usable else ""
        origin = "api_detail" if best else ""

        # If the detail payload is missing or only a short excerpt, consult
        # the public project page (which also covers the case where the
        # detail endpoint repeats the client name).
        if not best or len(collapse(best)) <= _SHORT_REAL_DESCRIPTION_CHARS:
            live = await self._fetch_live_description(project, detail)
            if (
                live
                and _is_usable(live, placeholders)
                and len(collapse(live)) >= len(collapse(best))
            ):
                best, origin = live, "live_page"

        if not best or _comparable(best) in placeholders:
            print(
                f"[FREEHUB RECOVERY] {uid}: could not recover suspicious "
                f"description {collapse(project.get('description'))[:80]!r}"
            )
            return

        if _comparable(best) == original_comparable:
            return

        if not original_is_placeholder and len(collapse(best)) <= len(collapse(original)):
            # Keep the original rather than replacing a plausible short
            # brief with a shorter (possibly partial) fallback.
            return

        _apply_description(project, best, origin)

    async def _fetch_detail(self, uid):
        try:
            payload = await self._client.fetch_project(uid)
        except Exception as exc:
            print(f"[FREEHUB RECOVERY] {uid}: detail fetch failed: {type(exc).__name__}: {exc}")
            return {}
        if not isinstance(payload, dict):
            return {}
        item = payload.get("item")
        if isinstance(item, dict):
            return item
        return payload

    async def _fetch_live_description(self, project, detail):
        url = project.get("project_link") or ""
        if isinstance(detail, dict):
            links = detail.get("links")
            if isinstance(links, dict) and links.get("html"):
                url = links["html"]
        if not _is_kafiil_url(url):
            return ""
        try:
            page = await self._client.fetch_text(url, headers=_HTML_HEADERS)
        except Exception as exc:
            print(
                f"[FREEHUB RECOVERY] {project.get('uid')}: page fetch failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return ""
        return extract_kafiil_description(page)


def _apply_description(project, description, origin):
    cleaned = clean_text(description)
    if not cleaned:
        return
    project["description"] = cleaned
    project["raw_text"] = f"{project.get('title', '')}\n\n{cleaned}"
    project["_description_recovered_from"] = origin
    print(
        f"[FREEHUB RECOVERY] {project.get('uid')}: recovered description "
        f"from {origin} ({len(collapse(cleaned))} chars)"
    )
