import asyncio
import re
import time

import aiohttp
from howlongtobeatpy.HTMLRequests import HTMLRequests, SearchModifiers
from howlongtobeatpy.JSONResultParser import JSONResultParser

MIN_SIMILARITY = 0.4

# HLTB's own search returns zero results (server-side, not a similarity/threshold
# issue) for queries containing a trademark/registered symbol (as a unicode
# glyph or spelled out, e.g. "(R)"), a standalone " - " dash separator, or a
# trailing parenthetical suffix — all common in Steam's store names:
# "Ace Combat® 7", "Agatha Christie - Murder on the Orient Express",
# "Age of Empires (R) III (2007)". Stripping/normalizing those before
# searching fixes matching for otherwise-identical titles.
_TRADEMARK_SYMBOLS_RE = re.compile(r"[™®©℠]")
_TEXTUAL_TRADEMARK_RE = re.compile(r"\((?:r|tm|c)\)", re.IGNORECASE)
_DASH_SEPARATOR_RE = re.compile(r"\s+-\s+")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_search_query(name: str) -> str:
    name = _TRADEMARK_SYMBOLS_RE.sub("", name)
    name = _TEXTUAL_TRADEMARK_RE.sub("", name)
    while True:
        # Repeat to catch several trailing groups, e.g. "Title (R) (2007)".
        stripped = _TRAILING_PAREN_RE.sub("", name)
        if stripped == name:
            break
        name = stripped
    name = _DASH_SEPARATOR_RE.sub(" ", name)
    return _WHITESPACE_RE.sub(" ", name).strip()

# howlongtobeatpy re-discovers HLTB's search endpoint + auth token from scratch
# on every single call (home page + JS chunk scrape + token fetch + the actual
# search — 4 requests per game). Under our sync's concurrency that hammers
# howlongtobeat.com hard enough to cause sporadic failures. The discovered
# search URL + token are reusable across many searches (verified empirically),
# so we cache them here and only pay the 3-request discovery cost occasionally
# instead of once per game.
_AUTH_TTL_SECONDS = 20 * 60
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_cache_lock = asyncio.Lock()
_cache: dict = {"auth_struct": None, "fetched_at": 0.0}


async def _refresh_auth() -> None:
    search_info = await HTMLRequests.async_send_website_request_getcode(_USER_AGENT)
    search_url = search_info.search_url if search_info else None
    auth_struct = await HTMLRequests.async_send_website_get_auth_token(search_url, _USER_AGENT)
    if search_url:
        HTMLRequests.SEARCH_URL = HTMLRequests.BASE_URL + search_url.lstrip("/")
    _cache["auth_struct"] = auth_struct
    _cache["fetched_at"] = time.monotonic()


async def _ensure_auth() -> None:
    async with _cache_lock:
        stale = time.monotonic() - _cache["fetched_at"] > _AUTH_TTL_SECONDS
        if _cache["auth_struct"] is None or stale:
            await _refresh_auth()


async def _raw_search(game_name: str, retry: bool = True) -> str | None:
    await _ensure_auth()
    auth_struct = _cache["auth_struct"]
    headers = HTMLRequests.get_search_request_headers(auth_struct, _USER_AGENT)
    payload = HTMLRequests.get_search_request_data(game_name, SearchModifiers.NONE, 1, auth_struct)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession() as session:
        async with session.post(HTMLRequests.SEARCH_URL, headers=headers, data=payload, timeout=timeout) as resp:
            if resp.status == 200:
                return await resp.text()

    if retry:
        # Cached token may have gone stale (or the request just hiccuped) —
        # force one fresh discovery and try exactly once more.
        async with _cache_lock:
            _cache["auth_struct"] = None
        return await _raw_search(game_name, retry=False)
    return None


async def _parse_search(query: str, similarity_reference_name: str) -> list:
    html_result = await _raw_search(query)
    if html_result is None:
        return []
    # minimum_similarity=0.0 makes the parser keep every candidate (with its
    # real similarity score) instead of silently dropping weak matches.
    # Case-insensitive: Steam names are sometimes ALL CAPS (e.g. "ANTONBLAST")
    # while HLTB's own title is normally-cased ("Antonblast") — a case-sensitive
    # diff between those tanks the similarity score for an otherwise perfect match.
    parser = JSONResultParser(
        similarity_reference_name, HTMLRequests.GAME_URL, 0.0, input_similarity_case_sensitive=False
    )
    parser.parse_json_result(html_result)
    return parser.results


async def _search_raw_results(game_name: str) -> list:
    full_name = _normalize_search_query(game_name or "")
    if not full_name:
        return []

    results = await _parse_search(full_name, full_name)
    if results:
        return results

    # HLTB's search appears to require every query word to literally appear
    # in the matched title (no fuzzy tolerance at the search-engine level,
    # only in our own similarity ranking afterwards) — a release/edition
    # qualifier Steam tacks on that HLTB's title doesn't share ("GOTY Edition",
    # "Definitive Edition"...) makes the whole search come back empty instead
    # of degrading gracefully. Retry with the query progressively shortened
    # from the end until something comes back, still scoring similarity
    # against the full original name so disambiguation isn't affected.
    # Never trim below 2 remaining words: a single leftover word (e.g. "Half-
    # Life" once "GOTY" is dropped from "Half-Life GOTY") is too generic to
    # disambiguate against an entire franchise of similarly-prefixed titles
    # by character similarity alone — better to find nothing here and leave
    # it for a manual match than risk confidently picking the wrong sequel.
    words = full_name.split(" ")
    for cut in range(1, len(words) - 1):
        trimmed = " ".join(words[: len(words) - cut])
        if len(trimmed) < 3:
            break
        results = await _parse_search(trimmed, full_name)
        if results:
            return results

    return []


def _has_main_story(entry) -> bool:
    return bool(entry.main_story and entry.main_story > 0)


def _ranking_key(entry):
    # HLTB occasionally has duplicate entries for the same title — one with
    # real community-submitted times, one freshly created with none yet —
    # both scoring an identical (often perfect) text similarity. Breaking
    # ties purely by whichever happened to come first in HLTB's own result
    # order picked the empty duplicate about as often as the useful one
    # (confirmed in practice: "Unbeatable" had a 100%-similarity empty twin
    # that won the tie before this fix). Prefer the one with actual data.
    return (entry.similarity, _has_main_story(entry))


async def search_hltb(name: str):
    results = await _search_raw_results(name)
    if not results:
        return None
    best = max(results, key=_ranking_key)
    if best.similarity < MIN_SIMILARITY:
        return None
    return best


async def search_hltb_candidates(query: str, limit: int = 8) -> list:
    """Manual-search variant for the admin page: no similarity cutoff, several results."""
    results = await _search_raw_results(query)
    if not results:
        return []
    results = sorted(results, key=_ranking_key, reverse=True)
    return results[:limit]
