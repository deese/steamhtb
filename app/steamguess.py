import random
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import steam_client, sync
from .config import BASE_DIR
from .db import engine, get_session
from .models import Game
from .queries import get_tag_counts

router = APIRouter(prefix="/steamguess")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

REVIEW_DESC_ORDER = [
    "Overwhelmingly Positive",
    "Very Positive",
    "Mostly Positive",
    "Positive",
    "Mixed",
    "Mostly Negative",
    "Negative",
    "Overwhelmingly Negative",
]

MAX_CANDIDATE_LOOKUPS = 40  # cap on-demand appdetails fetches per roll

# Non-overlapping so a multi-select with OR logic stays intuitive (checking
# "< 4h" and "60h+" means "either very short or very long", not something
# that double-counts games sitting in the middle).
DURATION_BUCKETS: dict[str, tuple[float, float | None]] = {
    "lt4": (0, 4),
    "4-8": (4, 8),
    "8-16": (8, 16),
    "16-24": (16, 24),
    "24-48": (24, 48),
    "48-60": (48, 60),
    "60plus": (60, None),
}
DURATION_LABELS: dict[str, str] = {
    "lt4": "< 4h",
    "4-8": "4–08h",
    "8-16": "8–16h",
    "16-24": "16–24h",
    "24-48": "24–48h",
    "48-60": "48–60h",
    "60plus": "60h+",
    "nodata": "Sin duración conocida",
}


def _game_genres(game: Game) -> list[str]:
    return [g.strip() for g in (game.genres or "").split(",") if g.strip()]


def _duration_bucket(hours: float | None) -> str:
    if hours is None:
        return "nodata"
    for key, (lo, hi) in DURATION_BUCKETS.items():
        if hours >= lo and (hi is None or hours < hi):
            return key
    return "nodata"


def _matches_duration(game: Game, buckets: set[str]) -> bool:
    if not buckets:
        return True
    return _duration_bucket(game.hltb_main) in buckets


def _to_json(game: Game) -> dict:
    return {
        "appid": game.appid,
        "name": game.name,
        "hours": round(game.playtime_minutes / 60, 2),
        "cover_url": f"https://cdn.akamai.steamstatic.com/steam/apps/{game.appid}/library_600x900.jpg",
        "header_url": game.header_image,
        "trailer_url": game.trailer_url,
        "genres": _game_genres(game) or ["Otros"],
        "tags": [t.name for t in game.tags][:6],
        "store_url": game.store_url,
        "release_date": game.release_date,
        "review_desc": game.review_score_desc,
        "review_count": (game.review_positive or 0) + (game.review_negative or 0),
        "metacritic_score": game.metacritic_score,
        "hltb_main": game.hltb_main,
        "hltb_main_extra": game.hltb_main_extra,
        "hltb_completionist": game.hltb_completionist,
    }


async def _ensure_checked(client: httpx.AsyncClient, appid: int) -> tuple[str | None, str | None] | None:
    """Lazily fetch steam_type + trailer_url for one game and persist them.
    Only called for games nobody has rolled before — cheap after the first
    few rolls since it's cached from then on.

    Returns (steam_type, trailer_url) rather than the Game row itself: the
    write happens in its own short-lived session, and the caller already
    holds its own (still-open) copy of the row to update in place — handing
    back an ORM object from a closed session would detach it and blow up on
    the next lazy-loaded attribute access (e.g. `.tags`).
    """
    await sync.steam_limiter.wait()
    details = await steam_client.fetch_app_details(client, appid)

    async with sync.db_write_lock:
        with Session(engine) as session:
            game = session.get(Game, appid)
            if not game:
                return None
            if details:
                game.steam_type = details.get("type") or game.steam_type
                game.trailer_url = steam_client.pick_trailer_url(details)
            game.trailer_checked_at = datetime.now(timezone.utc)
            session.add(game)
            session.commit()
            return game.steam_type, game.trailer_url


async def roll_games(
    session: Session,
    *,
    count: int,
    max_hours: float,
    genres: list[str],
    ratings: list[str],
    durations: list[str],
    tags: list[str],
) -> list[Game]:
    max_minutes = int(max_hours * 60)
    stmt = select(Game).where(Game.playtime_minutes <= max_minutes)
    candidates = list(session.exec(stmt).unique().all())

    if genres:
        genre_set = {g.lower() for g in genres}
        candidates = [g for g in candidates if any(x.lower() in genre_set for x in _game_genres(g))]
    if tags:
        tag_set = {t.lower() for t in tags}
        candidates = [
            g for g in candidates if any(t.name.lower() in tag_set for t in g.tags)
        ]
    if ratings:
        rating_set = {r.lower() for r in ratings}
        candidates = [g for g in candidates if (g.review_score_desc or "").lower() in rating_set]
    if durations:
        duration_set = set(durations)
        candidates = [g for g in candidates if _matches_duration(g, duration_set)]

    random.shuffle(candidates)

    results: list[Game] = []
    lookups = 0
    async with httpx.AsyncClient(headers={"User-Agent": "steamhtb/1.0"}) as client:
        for game in candidates:
            if len(results) >= count:
                break
            if game.steam_type in steam_client.NON_GAME_STEAM_TYPES:
                continue
            if game.trailer_checked_at is None:
                if lookups >= MAX_CANDIDATE_LOOKUPS:
                    # Don't stall a roll indefinitely on a library that's
                    # never been rolled before — take what's checked so far,
                    # the rest fills in on subsequent rolls.
                    continue
                lookups += 1
                checked = await _ensure_checked(client, game.appid)
                if checked is None:
                    continue
                game.steam_type, game.trailer_url = checked
                if game.steam_type in steam_client.NON_GAME_STEAM_TYPES:
                    continue
            results.append(game)
    return results


@router.get("", response_class=HTMLResponse)
async def steamguess_page(request: Request):
    return templates.TemplateResponse(request, "steamguess.html", {})


@router.get("/api/roll")
async def api_roll(
    count: int = 20,
    max_hours: float = 5.0,
    genres: str = "",
    ratings: str = "",
    durations: str = "",
    tags: str = "",
    session: Session = Depends(get_session),
):
    genre_list = [g.strip() for g in genres.split(",") if g.strip()]
    rating_list = [r.strip() for r in ratings.split(",") if r.strip()]
    duration_list = [d.strip() for d in durations.split(",") if d.strip()]
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    games = await roll_games(
        session,
        count=max(1, count),
        max_hours=max_hours,
        genres=genre_list,
        ratings=rating_list,
        durations=duration_list,
        tags=tag_list,
    )
    return JSONResponse([_to_json(g) for g in games])


@router.get("/api/genres")
async def api_genres(session: Session = Depends(get_session)):
    counts: dict[str, int] = {}
    for game in session.exec(select(Game)).all():
        for genre in _game_genres(game):
            counts[genre] = counts.get(genre, 0) + 1
    items = [{"name": name, "count": n} for name, n in sorted(counts.items())]
    return JSONResponse(items)


@router.get("/api/tags")
async def api_tags(session: Session = Depends(get_session)):
    tags = get_tag_counts(session)
    return JSONResponse([{"name": name, "count": count} for name, count in tags])


@router.get("/api/durations")
async def api_durations(session: Session = Depends(get_session)):
    counts: dict[str, int] = {key: 0 for key in list(DURATION_BUCKETS) + ["nodata"]}
    for game in session.exec(select(Game)).all():
        counts[_duration_bucket(game.hltb_main)] += 1
    order = list(DURATION_BUCKETS) + ["nodata"]
    items = [
        {"name": key, "label": DURATION_LABELS[key], "count": counts[key]}
        for key in order
        if counts[key] > 0
    ]
    return JSONResponse(items)


@router.get("/api/ratings")
async def api_ratings(session: Session = Depends(get_session)):
    counts: dict[str, int] = {}
    for game in session.exec(select(Game)).all():
        if game.review_score_desc:
            counts[game.review_score_desc] = counts.get(game.review_score_desc, 0) + 1
    items = [
        {"name": name, "count": counts[name]} for name in REVIEW_DESC_ORDER if name in counts
    ]
    return JSONResponse(items)
