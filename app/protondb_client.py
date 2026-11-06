import httpx

PROTONDB_URL = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"

# Higher = better. Used for sorting/filtering; "pending" (too few reports)
# deliberately has no rank, it isn't a verdict yet.
TIER_RANK = {"borked": 1, "bronze": 2, "silver": 3, "gold": 4, "platinum": 5, "native": 6}


async def fetch_summary(client: httpx.AsyncClient, appid: int) -> dict | None:
    """ProtonDB summary for a Steam appid.

    Returns the summary dict, ``{}`` when ProtonDB has no entry for the game
    (404 — a definitive "we looked, nothing there"), and ``None`` on a
    transient failure so the caller retries on a later sync.
    """
    resp = await client.get(PROTONDB_URL.format(appid=appid), timeout=15)
    if resp.status_code == 404:
        return {}
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
