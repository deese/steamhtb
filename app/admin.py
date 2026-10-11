from datetime import datetime, timezone

import httpx
import vdf
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import sync
from .config import BASE_DIR
from .db import get_session
from .models import Game
from .queries import (
    get_games_hltb_found_without_duration,
    get_games_hltb_low_confidence,
    get_games_hltb_unavailable,
    get_games_missing_hltb,
    get_games_missing_steamspy,
)
from .hltb_client import search_hltb, search_hltb_candidates

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def _parse_float(value: str) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


@router.get("", response_class=HTMLResponse)
async def admin_home(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "missing_hltb": get_games_missing_hltb(session),
            "no_duration_hltb": get_games_hltb_found_without_duration(session),
            "unavailable_hltb": get_games_hltb_unavailable(session),
            "low_confidence_hltb": get_games_hltb_low_confidence(session),
            "missing_steamspy": get_games_missing_steamspy(session),
        },
    )


@router.post("/hltb/retry-all", response_class=HTMLResponse)
async def admin_hltb_retry_all(request: Request):
    sync.start_hltb_retry()
    return templates.TemplateResponse(
        request, "partials/admin_hltb_retry_status.html", {"retry_status": sync.hltb_retry_status}
    )


@router.get("/hltb/retry-all/status", response_class=HTMLResponse)
async def admin_hltb_retry_all_status(request: Request):
    response = templates.TemplateResponse(
        request, "partials/admin_hltb_retry_status.html", {"retry_status": sync.hltb_retry_status}
    )
    if sync.hltb_retry_status["state"] in ("done", "error"):
        response.headers["HX-Trigger"] = "hltbListUpdated"
    return response


@router.get("/hltb/missing-list", response_class=HTMLResponse)
async def admin_hltb_missing_list(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "partials/admin_hltb_missing_list.html", {"missing_hltb": get_games_missing_hltb(session)}
    )


@router.get("/hltb/search", response_class=HTMLResponse)
async def admin_hltb_search(request: Request, appid: int, q: str = ""):
    candidates = await search_hltb_candidates(q)
    return templates.TemplateResponse(
        request,
        "partials/admin_hltb_results.html",
        {"appid": appid, "candidates": candidates},
    )


@router.get("/hltb/game-search", response_class=HTMLResponse)
async def admin_hltb_game_search(request: Request, q: str = "", session: Session = Depends(get_session)):
    """Look up any game in your own library by name, regardless of its
    current HLTB match state — the other sections only surface games in one
    specific (broken/uncertain) state, this is the general-purpose escape
    hatch for anything not caught by those."""
    games = []
    if q.strip():
        games = session.exec(select(Game).where(Game.name.ilike(f"%{q.strip()}%")).limit(20)).all()
    return templates.TemplateResponse(
        request, "partials/admin_hltb_search_results.html", {"games": games, "queried": bool(q.strip())}
    )


@router.post("/hltb/assign", response_class=HTMLResponse)
async def admin_hltb_assign(
    request: Request,
    appid: int = Form(...),
    hltb_id: int = Form(...),
    hltb_title: str = Form(""),
    hltb_main: str = Form(""),
    hltb_main_extra: str = Form(""),
    hltb_completionist: str = Form(""),
    hltb_similarity: str = Form(""),
    session: Session = Depends(get_session),
):
    game = session.get(Game, appid)
    if not game:
        raise HTTPException(status_code=404, detail="Juego no encontrado")

    game.hltb_id = hltb_id
    game.hltb_title = hltb_title or None
    game.hltb_main = _parse_float(hltb_main)
    game.hltb_main_extra = _parse_float(hltb_main_extra)
    game.hltb_completionist = _parse_float(hltb_completionist)
    game.hltb_similarity = _parse_float(hltb_similarity)
    game.hltb_unavailable = False
    game.hltb_manual_override = True
    game.hltb_synced_at = datetime.now(timezone.utc)

    async with sync.db_write_lock:
        session.add(game)
        session.commit()
        session.refresh(game)

    # Rendering the row here is safe for callers using hx-swap="delete" (htmx
    # removes the row and ignores the response body regardless) and lets
    # callers using hx-swap="outerHTML" (the general search tool) show the
    # updated state in place instead.
    return templates.TemplateResponse(request, "partials/admin_hltb_search_row.html", {"game": game})


@router.post("/hltb/unavailable", response_class=HTMLResponse)
async def admin_hltb_unavailable(
    request: Request,
    appid: int = Form(...),
    value: bool = Form(...),
    session: Session = Depends(get_session),
):
    game = session.get(Game, appid)
    if not game:
        raise HTTPException(status_code=404, detail="Juego no encontrado")

    game.hltb_unavailable = value
    game.hltb_manual_override = True

    async with sync.db_write_lock:
        session.add(game)
        session.commit()
        session.refresh(game)

    return templates.TemplateResponse(request, "partials/admin_hltb_search_row.html", {"game": game})


@router.post("/hltb/confirm", response_class=HTMLResponse)
async def admin_hltb_confirm(request: Request, appid: int = Form(...), session: Session = Depends(get_session)):
    """Dismiss a low-confidence match as actually correct, without changing
    any of its data — just stops it from being flagged for review again."""
    game = session.get(Game, appid)
    if not game:
        raise HTTPException(status_code=404, detail="Juego no encontrado")

    game.hltb_manual_override = True

    async with sync.db_write_lock:
        session.add(game)
        session.commit()
        session.refresh(game)

    return templates.TemplateResponse(request, "partials/admin_hltb_search_row.html", {"game": game})


@router.post("/hltb/recheck", response_class=HTMLResponse)
async def admin_hltb_recheck(
    request: Request, appid: int = Form(...), session: Session = Depends(get_session)
):
    """Re-run the auto search for a game HLTB already identified but had no
    submitted playtimes for — durations get crowd-sourced over time, so this
    can start returning real numbers later without anything else changing."""
    game = session.get(Game, appid)
    if not game:
        raise HTTPException(status_code=404, detail="Juego no encontrado")

    best = await search_hltb(game.name)
    if best:
        async with sync.db_write_lock:
            game.hltb_id = int(best.game_id) if best.game_id else None
            game.hltb_title = best.game_name
            game.hltb_main = best.main_story if best.main_story and best.main_story > 0 else None
            game.hltb_main_extra = (
                best.main_extra if best.main_extra and best.main_extra > 0 else None
            )
            game.hltb_completionist = (
                best.completionist if best.completionist and best.completionist > 0 else None
            )
            game.hltb_similarity = best.similarity
            game.hltb_synced_at = datetime.now(timezone.utc)
            session.add(game)
            session.commit()
            session.refresh(game)

    return templates.TemplateResponse(request, "partials/admin_hltb_no_duration_row.html", {"game": game})


@router.post("/steamspy/retry-all", response_class=HTMLResponse)
async def admin_steamspy_retry_all(request: Request, all: bool = False):
    sync.start_steamspy_retry(include_all=all)
    return templates.TemplateResponse(
        request, "partials/admin_steamspy_retry_status.html", {"retry_status": sync.steamspy_retry_status}
    )


@router.get("/steamspy/retry-all/status", response_class=HTMLResponse)
async def admin_steamspy_retry_all_status(request: Request):
    response = templates.TemplateResponse(
        request, "partials/admin_steamspy_retry_status.html", {"retry_status": sync.steamspy_retry_status}
    )
    if sync.steamspy_retry_status["state"] in ("done", "error"):
        response.headers["HX-Trigger"] = "steamspyListUpdated"
    return response


@router.get("/steamspy/missing-list", response_class=HTMLResponse)
async def admin_steamspy_missing_list(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "partials/admin_steamspy_missing_list.html",
        {"missing_steamspy": get_games_missing_steamspy(session)},
    )


@router.post("/steamspy/retry", response_class=HTMLResponse)
async def admin_steamspy_retry(
    request: Request, appid: int = Form(...), session: Session = Depends(get_session)
):
    game = session.get(Game, appid)
    if not game:
        raise HTTPException(status_code=404, detail="Juego no encontrado")

    async with httpx.AsyncClient(headers={"User-Agent": "steamhtb/1.0"}) as client:
        data = await sync.fetch_enrichment(client, appid, game.name, skip_hltb=True)

    tag_cache: dict[str, int] = {}
    async with sync.db_write_lock:
        sync.apply_enrichment(session, game, data, tag_cache)
        session.add(game)
        session.commit()
        session.refresh(game)

    return templates.TemplateResponse(request, "partials/admin_steamspy_row.html", {"game": game})


def _extract_apps_section(parsed: dict) -> dict | None:
    """Steam's localconfig.vdf nests the per-app data a few levels down, and
    capitalization has shifted across Steam client versions — walk case-
    insensitively instead of hardcoding one exact path."""
    node = parsed
    for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        if not isinstance(node, dict):
            return None
        match = next((v for k, v in node.items() if k.lower() == key.lower()), None)
        if match is None:
            return None
        node = match
    return node if isinstance(node, dict) else None


@router.post("/family/upload", response_class=HTMLResponse)
async def admin_family_upload(file: UploadFile, session: Session = Depends(get_session)):
    """Add any appid found in an uploaded Steam `localconfig.vdf` that isn't
    already in the library — Family Sharing games never show up in
    GetOwnedGames, but the Steam client tracks them locally since it needs
    per-app data (playtime, last played...) for anything you can launch,
    owned or borrowed. New rows start with a placeholder name; the next
    "Sincronizar nuevos" fills in the real name/tags/HLTB match like any
    other game (see run_sync / apply_enrichment)."""
    raw = await file.read()
    try:
        parsed = vdf.loads(raw.decode("utf-8", errors="ignore"))
    except Exception as exc:
        return HTMLResponse(
            f'<p class="text-sm text-red-400">No se pudo leer el fichero como VDF: {exc}</p>'
        )

    apps = _extract_apps_section(parsed)
    if not apps:
        return HTMLResponse(
            '<p class="text-sm text-red-400">No se encontró la sección de apps esperada — '
            "¿es el localconfig.vdf correcto?</p>"
        )

    existing_appids = set(session.exec(select(Game.appid)).all())
    now = datetime.now(timezone.utc)
    added = 0
    already_had = 0
    skipped = 0

    for appid_str, info in apps.items():
        try:
            appid = int(appid_str)
        except ValueError:
            skipped += 1
            continue
        if appid in existing_appids:
            already_had += 1
            continue

        playtime_minutes = 0
        if isinstance(info, dict):
            for key in ("Playtime", "playtime"):
                if key in info:
                    try:
                        playtime_minutes = int(info[key])
                    except (TypeError, ValueError):
                        playtime_minutes = 0
                    break

        session.add(
            Game(
                appid=appid,
                name=f"App {appid}",
                playtime_minutes=playtime_minutes,
                is_family_shared=True,
                header_image=f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
                store_url=f"https://store.steampowered.com/app/{appid}",
                owned_synced_at=now,
            )
        )
        existing_appids.add(appid)
        added += 1

    async with sync.db_write_lock:
        session.commit()

    return HTMLResponse(
        f'<p class="text-sm text-steam-accent2">'
        f"✓ {added} juegos nuevos de Family Sharing añadidos "
        f"({already_had} ya estaban, {skipped} entradas ignoradas). "
        f'Dale a "Sincronizar nuevos" en la librería para rellenar sus datos.</p>'
    )
