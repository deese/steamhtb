import httpx
from bs4 import BeautifulSoup

from .config import STEAM_API_KEY, STEAM_ID

OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
STORE_PAGE_URL = "https://store.steampowered.com/app/{appid}/"

# Regional age-rating boards, in order of preference for display.
RATING_SYSTEM_PRIORITY = ["pegi", "esrb", "usk", "dejus", "cero", "oflc", "kgrb", "gmedia", "csrr"]


class SteamClientError(RuntimeError):
    pass


async def fetch_owned_games(client: httpx.AsyncClient) -> list[dict]:
    if not STEAM_API_KEY or not STEAM_ID:
        raise SteamClientError("STEAM_API_KEY o STEAM_ID no configurados en .env")

    params = {
        "key": STEAM_API_KEY,
        "steamid": STEAM_ID,
        "format": "json",
        "include_appinfo": 1,
        "include_played_free_games": 1,
    }
    resp = await client.get(OWNED_GAMES_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", {}).get("games", [])


async def fetch_app_details(client: httpx.AsyncClient, appid: int) -> dict | None:
    params = {"appids": appid, "cc": "es", "l": "spanish"}
    resp = await client.get(APPDETAILS_URL, params=params, timeout=15)
    if resp.status_code != 200:
        return None
    payload = resp.json().get(str(appid))
    if not payload or not payload.get("success"):
        return None
    return payload.get("data")


async def fetch_review_summary(client: httpx.AsyncClient, appid: int) -> dict | None:
    """Steam's own (undocumented but public) review summary endpoint.

    Far more reliable than SteamSpy for recent/lower-profile games: SteamSpy has
    been stuck estimating from a limited sample since Valve locked down the full
    ownership API in 2018, so plenty of legitimately-reviewed games show 0/0
    there indefinitely. This endpoint reflects Steam's real, current counts.
    """
    params = {
        "json": 1,
        "language": "all",
        "purchase_type": "all",
        "num_per_page": 0,  # summary only, skip the review text payload
    }
    resp = await client.get(APPREVIEWS_URL.format(appid=appid), params=params, timeout=15)
    if resp.status_code != 200:
        return None
    payload = resp.json()
    if not payload.get("success"):
        return None
    return payload.get("query_summary")


async def fetch_store_page_tags(client: httpx.AsyncClient, appid: int) -> list[str]:
    """Fallback community-tag source for when SteamSpy has nothing.

    SteamSpy has been stuck sampling from a limited API since Valve locked
    down full ownership data in 2018 — plenty of lower-profile games never
    get tags there. Steam's own store page server-renders the same
    community tags (no JS needed to read them) in a `div.glance_tags`, so we
    can scrape those directly as a fallback. More fragile than a real API
    (HTML structure could change), so only used when SteamSpy comes up empty.
    """
    resp = await client.get(
        STORE_PAGE_URL.format(appid=appid),
        params={"l": "english"},
        headers={"Cookie": "birthtime=0; lastagecheckage=1-0-2000; wants_mature_content=1"},
        follow_redirects=True,
        timeout=15,
    )
    if resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    block = soup.find("div", class_="glance_tags")
    if not block:
        return []
    return [a.get_text(strip=True) for a in block.find_all("a", class_="app_tag") if a.get_text(strip=True)]


# Letter-graded boards that don't map onto a numeric scale by themselves.
# Values chosen to line up sensibly against PEGI's 3/7/12/16/18 bands, since
# PEGI is what most of a library ends up classified under.
_ESRB_NUMERIC = {"EC": 0, "E": 0, "E10": 10, "E10+": 10, "T": 13, "M": 17, "AO": 18}
_CERO_NUMERIC = {"A": 0, "B": 12, "C": 15, "D": 17, "Z": 18}
_OFLC_NUMERIC = {"G": 0, "PG": 8, "M": 15, "MA15+": 15, "R18+": 18}
# DEJUS/KGRB/others use plain numbers except for a "free for all ages" label.
_ALL_AGES_LABELS = {"L", "LIVRE", "ALL", "FREE"}


def numeric_age_from_rating(rating: str | None, system: str | None) -> int | None:
    """Normalize a rating board's value to a single comparable age number.

    NOT the same thing as Steam's `required_age` (the purchase-time gate,
    which stays 0 for almost every game regardless of actual content rating)
    — this is what age filtering should actually use.
    """
    if not rating:
        return None
    rating = rating.strip().upper()
    if rating in _ALL_AGES_LABELS:
        return 0

    try:
        return int(rating.rstrip("+"))
    except ValueError:
        pass

    system = (system or "").upper()
    table = {"ESRB": _ESRB_NUMERIC, "CERO": _CERO_NUMERIC, "OFLC": _OFLC_NUMERIC}.get(system)
    if table and rating in table:
        return table[rating]

    return None


def pick_trailer_url(details: dict) -> str | None:
    """Pick a playable trailer URL out of a Steam appdetails `movies` entry.

    Prefers the highlighted trailer, then HLS/DASH manifests over legacy
    direct MP4/WebM links (HLS lets the player pick a bitrate/adapt).
    """

    def _pick(movie: dict) -> str | None:
        mp4 = movie.get("mp4") or {}
        webm = movie.get("webm") or {}
        for url in (
            movie.get("hls_h264"),
            movie.get("dash_h264"),
            mp4.get("max"),
            mp4.get("480"),
            webm.get("max"),
            webm.get("480"),
        ):
            if url:
                return url
        return None

    movies = details.get("movies") or []
    for movie in movies:
        if movie.get("highlight"):
            url = _pick(movie)
            if url:
                return url
    for movie in movies:
        url = _pick(movie)
        if url:
            return url
    return None


# Steam Store product types that aren't a playable base game — excluded from
# the backlog roulette (owning a DLC or demo doesn't mean "a game to play").
NON_GAME_STEAM_TYPES = {"demo", "playtest", "dlc", "mod", "application", "music", "video", "tool"}


def pick_age_rating(details: dict) -> tuple[str | None, str | None, int | None, int | None]:
    """Return (rating, rating_system, numeric_age, required_age) from a Steam appdetails payload."""
    required_age = None
    raw_age = details.get("required_age")
    if raw_age not in (None, ""):
        try:
            required_age = int(str(raw_age).replace("+", ""))
        except ValueError:
            required_age = None

    ratings = details.get("ratings") or {}
    board_order = RATING_SYSTEM_PRIORITY + [s for s in ratings if s not in RATING_SYSTEM_PRIORITY]
    for system in board_order:
        board = ratings.get(system)
        if board and board.get("rating"):
            rating = board["rating"].upper()
            return rating, system.upper(), numeric_age_from_rating(rating, system), required_age

    if required_age:
        return str(required_age), "STEAM", required_age, required_age

    return None, None, None, required_age
