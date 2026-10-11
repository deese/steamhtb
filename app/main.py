from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth, config, sync
from .admin import router as admin_router
from .admin import templates as admin_templates
from .steamguess import router as steamguess_router
from .steamguess import templates as steamguess_templates
from .config import BASE_DIR, IS_CONFIGURED
from .db import get_session, init_db
from .queries import (
    get_filtered_games,
    get_games_hltb_unavailable,
    get_games_missing_hltb,
    get_games_missing_steamspy,
    get_library_stats,
    get_tag_counts,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


class LoginRequiredMiddleware(BaseHTTPMiddleware):
    """Gates every route behind a login cookie when STEAMHTB_LOGIN is set.

    No-op (every request passes straight through) when login is disabled —
    e.g. a Tailscale-only deployment, where Tailscale itself is already the
    access control and this would just be redundant friction.
    """

    async def dispatch(self, request: Request, call_next):
        if not config.LOGIN_ENABLED:
            return await call_next(request)
        path = request.url.path
        if path == "/login" or path.startswith("/static"):
            return await call_next(request)
        if auth.verify_session_cookie(request.cookies.get(auth.COOKIE_NAME)):
            return await call_next(request)
        return RedirectResponse(url=f"/login?next={path}", status_code=302)


app = FastAPI(title="Steam x HowLongToBeat Library", lifespan=lifespan)
app.add_middleware(LoginRequiredMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.include_router(admin_router)
app.include_router(steamguess_router)
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

for _templates in (templates, admin_templates, steamguess_templates):
    _templates.env.globals["login_enabled"] = config.LOGIN_ENABLED


def format_count(n: int | None) -> str:
    if n is None:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


templates.env.filters["format_count"] = format_count


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, session: Session = Depends(get_session)):
    games = get_filtered_games(session)
    tags = get_tag_counts(session)
    stats = get_library_stats(session)
    admin_pending = (
        len(get_games_missing_hltb(session))
        + len(get_games_hltb_unavailable(session))
        + len(get_games_missing_steamspy(session))
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "games": games,
            "tags": tags,
            "stats": stats,
            "status": sync.status,
            "configured": IS_CONFIGURED,
            "admin_pending": admin_pending,
        },
    )


@app.get("/games", response_class=HTMLResponse)
async def games_partial(
    request: Request,
    q: str = "",
    tags: list[str] = Query(default=[]),
    bucket: str = "any",
    age: str = "any",
    beaten: str = "any",
    family: str = "any",
    sort: str = "name_asc",
    session: Session = Depends(get_session),
):
    games = get_filtered_games(
        session, q=q, tag_names=tags, bucket=bucket, age=age, beaten=beaten, family=family, sort=sort
    )
    return templates.TemplateResponse(
        request,
        "partials/game_grid.html",
        {"games": games},
    )


@app.post("/sync", response_class=HTMLResponse)
async def start_sync(request: Request, force: bool = False):
    sync.start(force=force)
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        {"status": sync.status},
    )


@app.get("/sync/status", response_class=HTMLResponse)
async def sync_status(request: Request):
    response = templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        {"status": sync.status},
    )
    if sync.should_notify_grid():
        response.headers["HX-Trigger"] = "gamesUpdated"
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    next_url = str(form.get("next") or "/")
    if not auth.verify_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next_url, "error": "Contraseña incorrecta"},
            status_code=401,
        )
    response = RedirectResponse(url=next_url, status_code=302)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_cookie(),
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(auth.COOKIE_NAME)
    return response
