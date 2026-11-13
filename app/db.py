from sqlalchemy import text

from sqlmodel import SQLModel, Session, create_engine, select

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)

# SQLite doesn't support adding columns via create_all() on an existing table.
# Track new Game columns here (name -> DDL type/default) so upgrading an
# existing database doesn't require dropping it.
_GAME_COLUMN_MIGRATIONS = {
    "hltb_title": "VARCHAR",
    "hltb_manual_override": "BOOLEAN NOT NULL DEFAULT 0",
    "hltb_unavailable": "BOOLEAN NOT NULL DEFAULT 0",
    "age_rating_numeric": "INTEGER",
    "review_score_desc": "VARCHAR",
    "steam_type": "VARCHAR",
    "trailer_url": "VARCHAR",
    "trailer_checked_at": "DATETIME",
    "release_date_iso": "DATE",
    "protondb_tier": "VARCHAR",
    "protondb_rank": "INTEGER",
    "protondb_score": "FLOAT",
    "protondb_total": "INTEGER",
    "protondb_synced_at": "DATETIME",
    "play_status": "VARCHAR",
}

# Indexes for columns the filters/sorts hit. create_all() only indexes brand
# new tables, so existing databases get them here. Names match what SQLModel
# generates (ix_<table>_<column>) so both paths agree.
_INDEXES = {
    "game": [
        "playtime_minutes",
        "age_rating_numeric",
        "metacritic_score",
        "review_percent",
        "hltb_main",
        "protondb_rank",
        "release_date_iso",
        "play_status",
    ],
    "gametaglink": ["tag_id"],
}


def _migrate_game_columns() -> None:
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(game)"))}
        for column, ddl_type in _GAME_COLUMN_MIGRATIONS.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE game ADD COLUMN {column} {ddl_type}"))


def _ensure_indexes() -> None:
    with engine.begin() as conn:
        for table, columns in _INDEXES.items():
            for column in columns:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))


def _backfill_release_dates() -> None:
    """Parse release_date text for rows that predate release_date_iso. Purely
    local (no network); rows whose text isn't a concrete date stay NULL and
    are simply re-tried on the next start, which is cheap."""
    from .dates import parse_release_date
    from .models import Game

    with Session(engine) as session:
        games = session.exec(
            select(Game).where(Game.release_date_iso.is_(None)).where(Game.release_date.is_not(None))
        ).all()
        changed = False
        for game in games:
            parsed = parse_release_date(game.release_date)
            if parsed:
                game.release_date_iso = parsed
                session.add(game)
                changed = True
        if changed:
            session.commit()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_game_columns()
    _ensure_indexes()
    _backfill_release_dates()


def get_session():
    with Session(engine) as session:
        yield session
