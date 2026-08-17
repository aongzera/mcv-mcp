"""Settings and on-disk state locations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE = "https://www.mycourseville.com"

# MCV's own web client kicks off login through its first-party OAuth client; hitting this
# first is what puts the session into the right state for the chosen login page.
# NOTE: this request must NOT carry X-Requested-With, or MCV answers 401 "Unauthorized."
# instead of redirecting to the login form.
def oauth_bootstrap(login_page: str | None = "itchula") -> str:
    url = (
        f"{BASE}/api/oauth/authorize"
        "?response_type=code"
        "&client_id=mycourseville.com"
        f"&redirect_uri={BASE}"
    )
    return f"{url}&login_page={login_page}" if login_page else url


CHULA_LOGIN = f"{BASE}/api/chulalogin"  # Chula SSO: fields _token/username/password
MCV_LOGIN = f"{BASE}/api/login"  # MCV account: fields _token/loginfield/name/password
HOME = f"{BASE}/?q=courseville"


def ajax(command: str) -> str:
    """URL for an internal AJAX command, e.g. ajax('course')."""
    return f"{BASE}/?q=courseville/ajax/{command}"


def _default_state_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "mcv_mcp"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mcv_mcp"


# Be a polite client: this is a university server and we are one student's personal tool.
MIN_POLL_MINUTES = 10
DEFAULT_POLL_MINUTES = 30
REQUEST_DELAY_SECONDS = 0.4


LOGIN_METHODS = ("itchula", "mcv")


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    state_dir: Path
    poll_minutes: int
    poll_enabled: bool
    login_method: str = "itchula"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mcv.db"

    @property
    def cookie_path(self) -> Path:
        return self.state_dir / "cookies.json"

    @property
    def download_dir(self) -> Path:
        return self.state_dir / "downloads"

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    @property
    def login_method_page(self) -> str | None:
        """The `login_page` bootstrap parameter for this login method.

        Chula SSO needs `login_page=itchula`; MCV's own account form is the default page,
        so it takes no parameter.
        """
        return "itchula" if self.login_method == "itchula" else None


def load_config(env_file: str | os.PathLike[str] | None = None) -> Config:
    """Read settings from the environment, loading a .env file if present.

    Missing credentials are tolerated here so the server can still start and report a
    useful message through `auth_status` instead of crashing at import time.
    """
    load_dotenv(env_file, override=False)

    raw_state = os.environ.get("MCV_STATE_DIR", "").strip()
    state_dir = Path(raw_state) if raw_state else _default_state_dir()

    try:
        poll_minutes = int(os.environ.get("MCV_POLL_MINUTES", DEFAULT_POLL_MINUTES))
    except ValueError:
        poll_minutes = DEFAULT_POLL_MINUTES
    poll_minutes = max(MIN_POLL_MINUTES, poll_minutes)

    poll_enabled = os.environ.get("MCV_POLL_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "",
    }

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "downloads").mkdir(parents=True, exist_ok=True)

    login_method = os.environ.get("MCV_LOGIN_METHOD", "itchula").strip().lower()
    if login_method not in LOGIN_METHODS:
        login_method = "itchula"

    return Config(
        username=os.environ.get("MCV_USERNAME", "").strip(),
        password=os.environ.get("MCV_PASSWORD", ""),
        state_dir=state_dir,
        poll_minutes=poll_minutes,
        poll_enabled=poll_enabled,
        login_method=login_method,
    )
