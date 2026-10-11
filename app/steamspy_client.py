import httpx

STEAMSPY_URL = "https://steamspy.com/api.php"


async def fetch_app_tags(client: httpx.AsyncClient, appid: int) -> dict | None:
    params = {"request": "appdetails", "appid": appid}
    resp = await client.get(STEAMSPY_URL, params=params, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data or data.get("name") is None:
        return None
    return data
