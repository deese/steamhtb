from sqlalchemy import func
from sqlmodel import Session, select

from .models import Game, GameTagLink, Tag

TIME_BUCKETS: dict[str, tuple[float | None, float | None]] = {
    "lt8": (0, 8),
    "8-15": (8, 15),
    "15-30": (15, 30),
    "30-50": (30, 50),
    "50plus": (50, None),
}

AGE_BUCKETS: dict[str, tuple[int, int]] = {
    "0-3": (0, 3),
    "7": (4, 7),
    "12": (8, 12),
    "16": (13, 16),
    "18": (17, 999),
}

SORT_OPTIONS: dict[str, tuple] = {
    "name_asc": (Game.name, "asc"),
    "playtime_desc": (Game.playtime_minutes, "desc"),
    "hltb_asc": (Game.hltb_main, "asc"),
    "hltb_desc": (Game.hltb_main, "desc"),
    "metacritic_desc": (Game.metacritic_score, "desc"),
    "reviews_desc": (Game.review_percent, "desc"),
}


def get_filtered_games(
    session: Session,
    q: str = "",
    tag_names: list[str] | None = None,
    bucket: str = "any",
    age: str = "any",
    beaten: str = "any",
    sort: str = "name_asc",
) -> list[Game]:
    stmt = select(Game)

    if q:
        stmt = stmt.where(Game.name.ilike(f"%{q}%"))

    if tag_names:
        subq = (
            select(GameTagLink.game_appid)
            .join(Tag, Tag.id == GameTagLink.tag_id)
            .where(Tag.name.in_(tag_names))
            .group_by(GameTagLink.game_appid)
            .having(func.count(func.distinct(Tag.name)) == len(tag_names))
        )
        stmt = stmt.where(Game.appid.in_(subq))

    if bucket == "nodata":
        stmt = stmt.where(Game.hltb_main.is_(None))
    elif bucket in TIME_BUCKETS:
        lo, hi = TIME_BUCKETS[bucket]
        stmt = stmt.where(Game.hltb_main.is_not(None))
        if lo is not None:
            stmt = stmt.where(Game.hltb_main >= lo)
        if hi is not None:
            stmt = stmt.where(Game.hltb_main < hi)

    if age == "unrated":
        stmt = stmt.where(Game.age_rating_numeric.is_(None))
    elif age in AGE_BUCKETS:
        lo, hi = AGE_BUCKETS[age]
        stmt = stmt.where(Game.age_rating_numeric.is_not(None))
        stmt = stmt.where(Game.age_rating_numeric >= lo)
        stmt = stmt.where(Game.age_rating_numeric <= hi)

    # No stored/calculated column needed — just compare the two existing
    # columns directly in the query. At this scale (a personal library, not
    # millions of rows) that's exactly as fast as a precomputed flag, with
    # none of the "keep it in sync whenever playtime or hltb_main change"
    # upkeep a stored column would need.
    if beaten in ("yes", "no"):
        # hltb_main should never be stored as exactly 0 (every write path
        # converts "no data" to NULL instead) but guard here too: a 0 would
        # make `played_hours >= hltb_main` trivially true for every game.
        stmt = stmt.where(Game.hltb_main.is_not(None))
        stmt = stmt.where(Game.hltb_main > 0)
        played_hours = Game.playtime_minutes / 60.0
        stmt = stmt.where(played_hours >= Game.hltb_main if beaten == "yes" else played_hours < Game.hltb_main)

    column, direction = SORT_OPTIONS.get(sort, SORT_OPTIONS["name_asc"])
    order = column.desc() if direction == "desc" else column.asc()
    # Secondary sort by name keeps ties stable and predictable.
    stmt = stmt.order_by(order.nullslast() if hasattr(order, "nullslast") else order, Game.name.asc())

    return list(session.exec(stmt).unique().all())


def get_tag_counts(session: Session, limit: int | None = None) -> list[tuple[str, int]]:
    # No limit by default: there are only a few hundred unique tags total
    # (cheap to load, it's just text), and a rare one like "Incremental"
    # with 3 games would otherwise sit below any frequency cutoff — not just
    # hidden from the list, but unfindable even by typing its exact name in
    # the picker's search box, since that only filters what's already loaded.
    stmt = (
        select(Tag.name, func.count(GameTagLink.game_appid).label("cnt"))
        .join(GameTagLink, GameTagLink.tag_id == Tag.id)
        .group_by(Tag.name)
        .order_by(func.count(GameTagLink.game_appid).desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def get_games_missing_hltb(session: Session) -> list[Game]:
    """Games with no matching HLTB entry identified at all — these need a manual search."""
    stmt = (
        select(Game)
        .where(Game.hltb_id.is_(None))
        .where(Game.hltb_unavailable == False)  # noqa: E712
        .order_by(Game.name.asc())
    )
    return list(session.exec(stmt).all())


def get_games_hltb_found_without_duration(session: Session) -> list[Game]:
    """Games where we identified the right HLTB entry, but it has no submitted
    playtimes yet (main_story == 0 there) — nothing to fix, HLTB's community
    just hasn't logged times for it. Purely informational / periodic retry."""
    stmt = (
        select(Game)
        .where(Game.hltb_id.is_not(None))
        .where(Game.hltb_main.is_(None))
        .where(Game.hltb_unavailable == False)  # noqa: E712
        .order_by(Game.name.asc())
    )
    return list(session.exec(stmt).all())


def get_games_hltb_unavailable(session: Session) -> list[Game]:
    stmt = select(Game).where(Game.hltb_unavailable == True).order_by(Game.name.asc())  # noqa: E712
    return list(session.exec(stmt).all())


def get_games_hltb_low_confidence(session: Session, threshold: float = 0.8) -> list[Game]:
    """Games matched to an HLTB entry, but with a low text-similarity score —
    sometimes that's just a naming quirk (case, "Edition", trademark symbols)
    and the match is fine; sometimes it's a real mismatch (a completely
    different game that happened to score just above the auto-accept
    threshold). Needs a human to look, which is exactly what this list is for.

    Excludes games a human has already reviewed via /admin (manual_override) —
    once confirmed or fixed, it shouldn't keep nagging regardless of the
    similarity number still on file.
    """
    stmt = (
        select(Game)
        .where(Game.hltb_main.is_not(None))
        .where(Game.hltb_similarity.is_not(None))
        .where(Game.hltb_similarity < threshold)
        .where(Game.hltb_manual_override == False)  # noqa: E712
        .order_by(Game.hltb_similarity.asc())
    )
    return list(session.exec(stmt).all())


def get_games_missing_steamspy(session: Session) -> list[Game]:
    """Games with no community tags from SteamSpy.

    Reviews come from Steam's own appreviews endpoint (always current), but
    tags are SteamSpy-only. SteamSpy has been stuck sampling from a limited
    API since Valve locked down full ownership data in 2018, so some games —
    especially lower-profile or newer ones — never get tags there, not just
    "not indexed yet."
    """
    stmt = (
        select(Game)
        .where(Game.steamspy_synced_at.is_not(None))
        .order_by(Game.name.asc())
    )
    games = session.exec(stmt).unique().all()
    return [g for g in games if not g.tags]


def get_library_stats(session: Session) -> dict:
    total_games = session.exec(select(func.count(Game.appid))).one()
    synced_games = session.exec(
        select(func.count(Game.appid)).where(Game.steam_details_synced_at.is_not(None))
    ).one()
    last_sync = session.exec(select(func.max(Game.steam_details_synced_at))).one()
    return {
        "total_games": total_games,
        "synced_games": synced_games,
        "last_sync": last_sync,
    }
