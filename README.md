# Steam × HowLongToBeat Library

A self-hosted webapp that pulls your Steam library (games, tags, reviews,
Metacritic score, age rating) and cross-references it with duration data from
[HowLongToBeat](https://howlongtobeat.com), so you can filter your backlog by
name, tags (multi-select), estimated story length, and age rating.

FastAPI + SQLite backend, server-rendered frontend with HTMX + Alpine.js +
Tailwind (no Node build step).

## Features

- **Library browser**: filter by name, tags (AND logic — multi-select),
  HowLongToBeat duration bucket, age rating, and whether you've likely beaten
  a game (playtime ≥ estimated main story length).
- **Steamguess**: a backlog-roulette page that picks a random game for you
  from filtered criteria (genre, age rating, duration, tags), with trailer
  previews on hover.
- **Admin page**: review and fix HowLongToBeat matches — search for a game and
  re-run the match, review low-confidence auto-matches, mark a game as
  unavailable on HLTB, and retry SteamSpy tag fetches in bulk.
- **Optional login gate**: a lightweight cookie-based password gate for when
  the app is reachable beyond a trusted network, with a long-lived session so
  you're not re-entering a password constantly.

## Data sources

- **Steam Web API** (`IPlayerService/GetOwnedGames`): library, playtime.
- **Steam Store** (`store.steampowered.com/api/appdetails` + store page scrape):
  cover art, genre, tags, Metacritic score, price, release date, age rating
  (PEGI/ESRB/USK/...).
- **SteamSpy** (`steamspy.com/api.php`): fallback tags and review stats when
  Steam's own data isn't enough.
- **HowLongToBeat** (via [`howlongtobeatpy`](https://pypi.org/project/howlongtobeatpy/)):
  main story, main+extra, and completionist durations.

## Setup

1. Copy `.env.example` to `.env` and fill in:
   ```
   STEAM_API_KEY=your_steam_web_api_key   # https://steamcommunity.com/dev/apikey
   STEAM_ID=your_steamid64                # https://steamid.io
   ```
2. Create a virtualenv and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Start the server:
   ```bash
   uvicorn app.main:app --reload
   ```
4. Open http://127.0.0.1:8000

## Usage

- **Sync new**: fetches your current library and enriches only the games that
  don't have data yet (fast on repeat runs).
- **Resync all**: re-fetches Steam Store, SteamSpy, and HowLongToBeat data for
  your *entire* library. Useful to refresh stale scores, reviews, or
  durations. Can take a while on large libraries (see below).
- Sidebar filters: name search, tags (multi-select, AND logic), HowLongToBeat
  duration bucket, age rating, "beaten" status, library origin, and several
  sort options.

### Why the first sync is slow

Steam Store and SteamSpy rate-limit anonymous requests (roughly 1 request/sec
each). The app respects those limits with its own rate limiter, so a library
of N games takes roughly N seconds on first sync. Later "Sync new" runs only
touch newly-added games, so they're nearly instant.

## Optional login

Off by default. Turn it on if the app will be reachable from outside a
network you already trust:

```
STEAMHTB_LOGIN=true
STEAMHTB_LOGIN_PASSWORD=choose_a_password
STEAMHTB_COOKIE_SECURE=true   # only once served over HTTPS
STEAMHTB_SESSION_DAYS=180     # optional, defaults to 180 (6 months)
```

Once logged in on a device, the session cookie lasts `STEAMHTB_SESSION_DAYS`
days (180 by default) — no repeated password prompts until it expires, you
clear it, or you change the password (which invalidates every existing
session). A "Log out" button appears top-right on every page when this is
enabled.

## Deploying with Docker

Tested with a real `docker build` + container run (routes `/`, `/admin`,
`/steamguess`, DB migration and volume persistence all verified).

```bash
docker compose up -d --build
```

- Environment variables: `STEAM_API_KEY`, `STEAM_ID`, plus the optional login
  variables above if you want the login gate on.
- `docker-compose.yml` is set up to join an external reverse-proxy network
  (`shared_proxy_network`) without publishing any port to the host — adjust
  this to however you route traffic to containers in your own setup (a
  different external network name, or a `ports:` mapping if you don't use a
  reverse proxy at all).
- SQLite data persists in the named volume `steamhtb_data`, independent of the
  container's lifecycle.

### Migrating an existing database

The container starts with an empty database on first run. To keep a library
you'd already synced outside Docker, copy your existing `steamhtb.db` into the
volume before the first start (or while the container is stopped):

```bash
docker volume create steamhtb_data
docker run --rm -v steamhtb_data:/data -v "$(pwd)/data:/src" alpine \
  cp /src/steamhtb.db /data/steamhtb.db
```

## Project structure

```
app/
  main.py             FastAPI routes
  config.py           .env loading
  db.py                SQLite engine / sessions
  models.py            SQLModel models (Game, Tag, GameTagLink)
  queries.py            filtering and sorting
  steam_client.py       Steam Web API + Store API + age rating parsing
  steamspy_client.py    SteamSpy tags and reviews (fallback)
  hltb_client.py         HowLongToBeat search/matching
  sync.py                sync orchestration (concurrent fetch, serialized
                         DB writes, live progress)
  ratelimit.py           simple rate limiter for external APIs
  admin.py               admin routes (HLTB review)
  steamguess.py          backlog-roulette feature
  templates/             Jinja2 + HTMX partials
data/
  steamhtb.db            SQLite (not versioned)
```

## Notes

- `.env` holds your Steam API key — don't commit it (already in
  `.gitignore`).
- Age rating isn't available for every game — many indie titles have no
  rating board entry on Steam and show up as "unrated".
