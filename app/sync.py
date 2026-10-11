import asyncio
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from . import hltb_client, steam_client, steamspy_client
from .db import engine
from .models import Game, GameTagLink, Tag
from .ratelimit import RateLimiter

steam_limiter = RateLimiter(1.2)
steam_reviews_limiter = RateLimiter(1.1)
steamspy_limiter = RateLimiter(1.05)

GAME_CONCURRENCY = 4

# SQLite only allows a single writer at a time; serialize all commits through
# this lock so concurrent game workers never hit "database is locked".
db_write_lock = asyncio.Lock()

status: dict = {
    "state": "idle",  # idle | running | done | error
    "total": 0,
    "processed": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "errors": [],
    "notified_at": 0,  # last `processed` count at which the grid was told to refresh
}

# Separate bulk action from the admin page: re-run the (thresholded, automatic)
# HLTB search for every game still missing a match. Games auto-sync skipped
# only because of a transient network/Cloudflare hiccup often resolve on retry
# without needing a manual search.
hltb_retry_status: dict = {
    "state": "idle",  # idle | running | done | error
    "total": 0,
    "processed": 0,
    "matched": 0,
    "current": "",
    "errors": [],
}

# Same idea for SteamSpy/store-page tags: re-run enrichment for every game
# still missing tags. Mainly useful right after the store-page fallback
# fetch is added/changed (or whenever you want to sweep every untagged game
# at once instead of clicking "Reintentar" one row at a time in /admin).
steamspy_retry_status: dict = {
    "state": "idle",  # idle | running | done | error
    "total": 0,
    "processed": 0,
    "matched": 0,
    "current": "",
    "errors": [],
}

# How many freshly-enriched games to accumulate before telling the grid to refresh.
NOTIFY_BATCH_SIZE = 20


def is_running() -> bool:
    return status["state"] == "running"


def should_notify_grid() -> bool:
    """Whether the game grid should be told to refresh right now.

    Fires once per sync completion, and periodically (every NOTIFY_BATCH_SIZE
    games) while a sync is running, so newly-enriched cards show up without
    waiting for the whole sync to finish.
    """
    if status["state"] in ("done", "error"):
        return True
    if status["state"] == "running" and status["processed"] - status["notified_at"] >= NOTIFY_BATCH_SIZE:
        status["notified_at"] = status["processed"]
        return True
    return False


def start(force: bool = False) -> bool:
    if is_running():
        return False
    status.update(
        state="running",
        total=0,
        processed=0,
        current="Obteniendo librería de Steam…",
        started_at=datetime.now(timezone.utc),
        finished_at=None,
        errors=[],
        notified_at=0,
    )
    asyncio.create_task(run_sync(force=force))
    return True


def _get_or_create_tag_id(session: Session, name: str, cache: dict[str, int]) -> int:
    if name in cache:
        return cache[name]
    tag = session.exec(select(Tag).where(Tag.name == name)).first()
    if not tag:
        tag = Tag(name=name)
        session.add(tag)
        session.flush()
    cache[name] = tag.id
    return tag.id


async def fetch_enrichment(client: httpx.AsyncClient, appid: int, name: str, skip_hltb: bool) -> dict:
    """Network-only phase, safe to run concurrently across games."""
    errors: list[str] = []

    await steam_limiter.wait()
    try:
        details = await steam_client.fetch_app_details(client, appid)
    except Exception as exc:
        details = None
        errors.append(f"{name}: steam store - {exc}")

    await steam_reviews_limiter.wait()
    try:
        reviews = await steam_client.fetch_review_summary(client, appid)
    except Exception as exc:
        reviews = None
        errors.append(f"{name}: steam reviews - {exc}")

    await steamspy_limiter.wait()
    try:
        spy = await steamspy_client.fetch_app_tags(client, appid)
    except Exception as exc:
        spy = None
        errors.append(f"{name}: steamspy - {exc}")

    # Steam's own store page is the primary tag source: it's what the Steam
    # client's own library tag filter reflects, and it can genuinely disagree
    # with SteamSpy (e.g. "Incremental" vs. SteamSpy's "Clicker" for the same
    # game, at the same rank) — not just have fewer tags. SteamSpy's tags are
    # only a fallback for when this scrape itself fails.
    await steam_limiter.wait()
    store_tags: list[str] | None = None
    try:
        store_tags = await steam_client.fetch_store_page_tags(client, appid)
    except Exception as exc:
        errors.append(f"{name}: store tags - {exc}")

    hltb = None
    if not skip_hltb:
        try:
            hltb = await hltb_client.search_hltb(name)
        except Exception as exc:
            errors.append(f"{name}: hltb - {exc}")

    return {
        "details": details,
        "reviews": reviews,
        "spy": spy,
        "store_tags": store_tags,
        "hltb": hltb,
        "skip_hltb": skip_hltb,
        "errors": errors,
    }


def apply_enrichment(session: Session, game: Game, data: dict, tag_cache: dict[str, int]) -> None:
    """DB-write phase. Caller must hold db_write_lock."""
    now = datetime.now(timezone.utc)

    details = data["details"]
    if details:
        # Games added via the Family Sharing upload start out with a
        # placeholder "App <appid>" name (they don't come from
        # GetOwnedGames, which is where every other game's real name comes
        # from) — this is what fills in the real one once enrichment runs.
        game.name = details.get("name") or game.name
        game.header_image = details.get("header_image") or game.header_image
        genres = details.get("genres") or []
        if genres:
            game.genres = ", ".join(g["description"] for g in genres)
        mc = details.get("metacritic")
        if mc:
            game.metacritic_score = mc.get("score")
            game.metacritic_url = mc.get("url")
        game.is_free = bool(details.get("is_free"))
        price = details.get("price_overview")
        game.price_final_cents = price.get("final") if price else None
        release = details.get("release_date") or {}
        game.release_date = release.get("date")

        rating, rating_system, age_numeric, required_age = steam_client.pick_age_rating(details)
        game.age_rating = rating
        game.age_rating_system = rating_system
        game.age_rating_numeric = age_numeric
        game.required_age = required_age

        game.steam_type = details.get("type") or game.steam_type
        game.trailer_url = steam_client.pick_trailer_url(details)
        game.trailer_checked_at = now
    game.steam_details_synced_at = now

    # Steam's own review summary is the source of truth (current, no sampling
    # gaps); SteamSpy only fills in if that call itself failed outright.
    reviews = data["reviews"]
    if reviews:
        positive = reviews.get("total_positive") or 0
        negative = reviews.get("total_negative") or 0
        game.review_positive = positive
        game.review_negative = negative
        total_reviews = positive + negative
        game.review_percent = round(positive / total_reviews * 100, 1) if total_reviews else None
        game.review_score_desc = reviews.get("review_score_desc") or game.review_score_desc

    spy = data["spy"]
    if spy:
        if not reviews:
            positive = spy.get("positive") or 0
            negative = spy.get("negative") or 0
            game.review_positive = positive
            game.review_negative = negative
            total_reviews = positive + negative
            game.review_percent = round(positive / total_reviews * 100, 1) if total_reviews else None
        if not game.genres:
            game.genres = spy.get("genre")

    # Steam's own store page is the primary tag source (see fetch_enrichment);
    # SteamSpy only fills in if that scrape itself failed outright. If *both*
    # sources failed (a transient network hiccup, say) keep whatever tags the
    # game already had rather than wiping them to nothing. No cap on count —
    # that's a display concern for the templates, not a storage one, and a
    # storage cap can silently drop a real tag (e.g. "Incremental" sitting at
    # rank 16 of 20 on Steam's own page).
    store_tag_names = data.get("store_tags") or None
    spy_tags_data = spy.get("tags") if spy else None
    spy_tag_names = list(spy_tags_data.keys()) if isinstance(spy_tags_data, dict) and spy_tags_data else None
    tag_names = store_tag_names or spy_tag_names

    if tag_names is not None:
        existing_links = session.exec(
            select(GameTagLink).where(GameTagLink.game_appid == game.appid)
        ).all()
        for link in existing_links:
            session.delete(link)
        session.flush()

        for tag_name in tag_names:
            tag_id = _get_or_create_tag_id(session, tag_name, tag_cache)
            session.add(GameTagLink(game_appid=game.appid, tag_id=tag_id))
    game.steamspy_synced_at = now

    # A manual admin decision (match or "unavailable") owns hltb_* from then on.
    if not data["skip_hltb"]:
        hltb = data["hltb"]
        if hltb:
            game.hltb_id = int(hltb.game_id) if hltb.game_id else None
            game.hltb_title = hltb.game_name
            game.hltb_main = hltb.main_story if hltb.main_story and hltb.main_story > 0 else None
            game.hltb_main_extra = hltb.main_extra if hltb.main_extra and hltb.main_extra > 0 else None
            game.hltb_completionist = (
                hltb.completionist if hltb.completionist and hltb.completionist > 0 else None
            )
            game.hltb_similarity = hltb.similarity
        game.hltb_synced_at = now


async def _enrich_game(
    client: httpx.AsyncClient, appid: int, name: str, tag_cache: dict[str, int], skip_hltb: bool
) -> None:
    data = await fetch_enrichment(client, appid, name, skip_hltb)
    if data["errors"]:
        status["errors"].extend(data["errors"])

    async with db_write_lock:
        with Session(engine) as session:
            game = session.get(Game, appid)
            if not game:
                return
            apply_enrichment(session, game, data, tag_cache)
            session.add(game)
            session.commit()

    status["processed"] += 1
    status["current"] = name


async def run_sync(force: bool = False) -> None:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "steamhtb/1.0"}) as client:
            try:
                owned = await steam_client.fetch_owned_games(client)
            except Exception as exc:
                status.update(state="error", errors=[str(exc)], finished_at=datetime.now(timezone.utc))
                return

            now = datetime.now(timezone.utc)
            with Session(engine) as session:
                for entry in owned:
                    appid = entry["appid"]
                    game = session.get(Game, appid)
                    if not game:
                        game = Game(appid=appid, name=entry.get("name", f"App {appid}"))
                    game.name = entry.get("name", game.name)
                    game.playtime_minutes = entry.get("playtime_forever", 0)
                    # GetOwnedGames is authoritative proof of direct ownership —
                    # always wins over a prior Family Sharing flag (e.g. you
                    # bought a game you used to only borrow).
                    game.is_family_shared = False
                    if not game.header_image:
                        game.header_image = (
                            f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"
                        )
                    game.store_url = f"https://store.steampowered.com/app/{appid}"
                    game.owned_synced_at = now
                    session.add(game)
                session.commit()

                if force:
                    targets = list(owned)
                else:
                    targets = [
                        entry
                        for entry in owned
                        if session.get(Game, entry["appid"]).steam_details_synced_at is None
                    ]

                # Family Sharing games (added via the /admin upload) never
                # come back from GetOwnedGames, so the loop above never sees
                # them — without this they'd sit untouched forever instead of
                # picking up real data on the next "Sincronizar nuevos".
                owned_appids = {entry["appid"] for entry in owned}
                family_query = select(Game).where(Game.is_family_shared == True)  # noqa: E712
                if not force:
                    family_query = family_query.where(Game.steam_details_synced_at.is_(None))
                family_games = session.exec(family_query).all()
                targets.extend(
                    {"appid": g.appid, "name": g.name}
                    for g in family_games
                    if g.appid not in owned_appids
                )

                skip_hltb_by_appid = {
                    entry["appid"]: bool(session.get(Game, entry["appid"]).hltb_manual_override)
                    for entry in targets
                }

            status["total"] = len(targets)
            status["current"] = ""

            tag_cache: dict[str, int] = {}
            semaphore = asyncio.Semaphore(GAME_CONCURRENCY)

            async def worker(entry: dict) -> None:
                async with semaphore:
                    await _enrich_game(
                        client,
                        entry["appid"],
                        entry.get("name", str(entry["appid"])),
                        tag_cache,
                        skip_hltb_by_appid[entry["appid"]],
                    )

            await asyncio.gather(*(worker(entry) for entry in targets))

        status["state"] = "done"
        status["finished_at"] = datetime.now(timezone.utc)
    except Exception as exc:  # last-resort guard so the UI never gets stuck on "running"
        status["state"] = "error"
        status["errors"].append(str(exc))
        status["finished_at"] = datetime.now(timezone.utc)


def is_hltb_retry_running() -> bool:
    return hltb_retry_status["state"] == "running"


def start_hltb_retry() -> bool:
    if is_hltb_retry_running():
        return False
    hltb_retry_status.update(state="running", total=0, processed=0, matched=0, current="", errors=[])
    asyncio.create_task(run_hltb_retry())
    return True


async def run_hltb_retry() -> None:
    try:
        with Session(engine) as session:
            games = session.exec(
                select(Game)
                .where(Game.hltb_main.is_(None))
                .where(Game.hltb_unavailable == False)  # noqa: E712
                .where(Game.hltb_manual_override == False)  # noqa: E712
            ).all()
            targets = [(g.appid, g.name) for g in games]

        hltb_retry_status["total"] = len(targets)

        limiter = RateLimiter(0.6)
        semaphore = asyncio.Semaphore(3)

        async def worker(appid: int, name: str) -> None:
            async with semaphore:
                await limiter.wait()
                try:
                    best = await hltb_client.search_hltb(name)
                except Exception as exc:
                    best = None
                    hltb_retry_status["errors"].append(f"{name}: {exc}")

                if best:
                    # Save the match even if HLTB has no submitted playtimes
                    # for it yet (main_story == 0) — that's a real match, just
                    # one where duration data hasn't been crowd-sourced yet.
                    # Recording hltb_id lets us tell "found, no data yet" apart
                    # from "never matched" instead of endlessly re-searching it.
                    async with db_write_lock:
                        with Session(engine) as session:
                            game = session.get(Game, appid)
                            if game and not game.hltb_manual_override:
                                game.hltb_id = int(best.game_id) if best.game_id else None
                                game.hltb_title = best.game_name
                                game.hltb_main = (
                                    best.main_story if best.main_story and best.main_story > 0 else None
                                )
                                game.hltb_main_extra = (
                                    best.main_extra if best.main_extra and best.main_extra > 0 else None
                                )
                                game.hltb_completionist = (
                                    best.completionist
                                    if best.completionist and best.completionist > 0
                                    else None
                                )
                                game.hltb_similarity = best.similarity
                                game.hltb_synced_at = datetime.now(timezone.utc)
                                session.add(game)
                                session.commit()
                    hltb_retry_status["matched"] += 1

                hltb_retry_status["processed"] += 1
                hltb_retry_status["current"] = name

        await asyncio.gather(*(worker(appid, name) for appid, name in targets))
        hltb_retry_status["state"] = "done"
    except Exception as exc:
        hltb_retry_status["state"] = "error"
        hltb_retry_status["errors"].append(str(exc))


def is_steamspy_retry_running() -> bool:
    return steamspy_retry_status["state"] == "running"


def start_steamspy_retry(include_all: bool = False) -> bool:
    if is_steamspy_retry_running():
        return False
    steamspy_retry_status.update(state="running", total=0, processed=0, matched=0, current="", errors=[])
    asyncio.create_task(run_steamspy_retry(include_all=include_all))
    return True


async def run_steamspy_retry(include_all: bool = False) -> None:
    try:
        with Session(engine) as session:
            games = session.exec(select(Game).where(Game.steamspy_synced_at.is_not(None))).unique().all()
            # include_all=True re-processes every game (e.g. after a change to
            # tag-fetching logic itself, like flipping the store-page/SteamSpy
            # priority) instead of only the ones currently missing tags.
            targets = [(g.appid, g.name) for g in games if include_all or not g.tags]

        steamspy_retry_status["total"] = len(targets)

        semaphore = asyncio.Semaphore(GAME_CONCURRENCY)
        tag_cache: dict[str, int] = {}

        async def worker(appid: int, name: str) -> None:
            async with semaphore:
                async with httpx.AsyncClient(headers={"User-Agent": "steamhtb/1.0"}) as client:
                    data = await fetch_enrichment(client, appid, name, skip_hltb=True)
                if data["errors"]:
                    steamspy_retry_status["errors"].extend(data["errors"])

                async with db_write_lock:
                    with Session(engine) as session:
                        game = session.get(Game, appid)
                        if game:
                            apply_enrichment(session, game, data, tag_cache)
                            session.add(game)
                            session.commit()
                            if game.tags:
                                steamspy_retry_status["matched"] += 1

                steamspy_retry_status["processed"] += 1
                steamspy_retry_status["current"] = name

        await asyncio.gather(*(worker(appid, name) for appid, name in targets))
        steamspy_retry_status["state"] = "done"
    except Exception as exc:
        steamspy_retry_status["state"] = "error"
        steamspy_retry_status["errors"].append(str(exc))
