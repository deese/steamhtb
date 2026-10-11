from datetime import datetime
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class GameTagLink(SQLModel, table=True):
    game_appid: int = Field(foreign_key="game.appid", primary_key=True)
    tag_id: int = Field(foreign_key="tag.id", primary_key=True)


class Tag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)

    games: list["Game"] = Relationship(back_populates="tags", link_model=GameTagLink)


class Game(SQLModel, table=True):
    appid: int = Field(primary_key=True)
    name: str = Field(index=True)

    playtime_minutes: int = 0
    header_image: Optional[str] = None
    store_url: Optional[str] = None
    # True for games found in an uploaded Steam localconfig.vdf that aren't
    # in your own GetOwnedGames response — i.e. played via Family Sharing,
    # not owned outright. Everything else (enrichment, filters, steamguess)
    # treats these exactly like any other game once the name/details fill in
    # through the normal sync.
    is_family_shared: bool = False

    genres: Optional[str] = None
    is_free: bool = False
    price_final_cents: Optional[int] = None
    release_date: Optional[str] = None

    age_rating: Optional[str] = None
    age_rating_system: Optional[str] = None
    # Normalized numeric age derived from age_rating/age_rating_system (PEGI,
    # ESRB, USK, DEJUS...) — this is what filtering uses. NOT the same as
    # required_age: Steam's own purchase-time age gate, which stays 0 for
    # almost everything regardless of the actual content rating (a PEGI 18
    # game like Darksiders has required_age=0 — Steam only gates checkout for
    # a small subset of explicitly adult content).
    age_rating_numeric: Optional[int] = None
    required_age: Optional[int] = None

    metacritic_score: Optional[int] = None
    metacritic_url: Optional[str] = None

    review_positive: Optional[int] = None
    review_negative: Optional[int] = None
    review_percent: Optional[float] = None
    # Steam's own descriptor ("Very Positive", "Mixed"...) from the same
    # appreviews call — used to group/filter in the "steamguess" roulette.
    review_score_desc: Optional[str] = None

    # Steam Store's own product type ("game", "dlc", "demo", "music"...).
    # The backlog roulette excludes anything that isn't a full game.
    steam_type: Optional[str] = None

    # Lazily fetched: the roulette only needs this for whatever games it
    # happens to roll, not the whole library, so it isn't part of the
    # regular sync pass. trailer_checked_at being set (even with a null URL)
    # means "we looked and there isn't one", vs "never checked".
    trailer_url: Optional[str] = None
    trailer_checked_at: Optional[datetime] = None

    hltb_id: Optional[int] = None
    hltb_title: Optional[str] = None
    hltb_main: Optional[float] = None
    hltb_main_extra: Optional[float] = None
    hltb_completionist: Optional[float] = None
    hltb_similarity: Optional[float] = None
    # Once True, auto-sync never touches hltb_* fields again for this game —
    # it's fully owned by the admin page (manual match or "unavailable" flag).
    hltb_manual_override: bool = False
    hltb_unavailable: bool = False

    owned_synced_at: Optional[datetime] = None
    steam_details_synced_at: Optional[datetime] = None
    steamspy_synced_at: Optional[datetime] = None
    hltb_synced_at: Optional[datetime] = None

    tags: list[Tag] = Relationship(back_populates="games", link_model=GameTagLink)
