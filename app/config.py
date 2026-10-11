import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
STEAM_ID = os.getenv("STEAM_ID", "").strip()

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'steamhtb.db'}")

IS_CONFIGURED = bool(STEAM_API_KEY and STEAM_ID)

# Optional cookie-based login gate — off by default (e.g. when the app is
# only reachable over Tailscale, which is already the access control).
# Set STEAMHTB_LOGIN=true when exposing it more broadly (e.g. via a reverse
# proxy with a real domain) and it isn't otherwise protected.
LOGIN_ENABLED = os.getenv("STEAMHTB_LOGIN", "").strip().lower() in ("1", "true", "yes", "on")
LOGIN_PASSWORD = os.getenv("STEAMHTB_LOGIN_PASSWORD", "").strip()
# Set to true only once you're sure the app is served over HTTPS (e.g.
# behind Nginx Proxy Manager) — over plain HTTP (like a bare Tailscale
# deployment) a Secure cookie would just never get sent back, breaking login.
COOKIE_SECURE = os.getenv("STEAMHTB_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")
# How long a login session cookie lasts before you need to log in again.
SESSION_DAYS = int(os.getenv("STEAMHTB_SESSION_DAYS", "180"))

if LOGIN_ENABLED and not LOGIN_PASSWORD:
    raise RuntimeError(
        "STEAMHTB_LOGIN está activado pero falta STEAMHTB_LOGIN_PASSWORD en el entorno."
    )
