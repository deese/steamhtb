from sqlalchemy import text

from sqlmodel import SQLModel, Session, create_engine

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
    "is_family_shared": "BOOLEAN NOT NULL DEFAULT 0",
}


def _migrate_game_columns() -> None:
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(game)"))}
        for column, ddl_type in _GAME_COLUMN_MIGRATIONS.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE game ADD COLUMN {column} {ddl_type}"))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_game_columns()


def get_session():
    with Session(engine) as session:
        yield session
