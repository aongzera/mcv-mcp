"""HTTP session against MyCourseVille: Chula SSO login + the internal AJAX endpoints."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .config import (
    BASE,
    CHULA_LOGIN,
    HOME,
    MCV_LOGIN,
    REQUEST_DELAY_SECONDS,
    Config,
    ajax,
    oauth_bootstrap,
)

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_TOKEN_RE = re.compile(r'name="_token"\s+value="(.+?)"')
# The dashboard's year/semester dropdown. `all-yearsem-select` is what MCV serves today;
# `student-yearsem-select` is kept as a fallback in case the id varies by account role.
_YEARSEM_BLOCK_RE = re.compile(
    r'<select[^>]*id="(?:all|student)-yearsem-select"[^>]*>[\s\S]*?</select>', re.I
)
_OPTION_VALUE_RE = re.compile(r'<option\s+value="([^"]+)"')
_CURRENT_YEARSEM_RE = re.compile(r'data-value="([^"]+)"')

# Courses everyone can see without being enrolled. Their presence proves nothing about
# whether we are logged in, so they are excluded from the login check.
_PUBLIC_COURSE_PREFIX = "CHULAMOOC"


class MCVError(RuntimeError):
    """Any failure talking to MyCourseVille."""


class NotLoggedIn(MCVError):
    """The session is missing or expired."""


class MCVClient:
    """A logged-in MyCourseVille session.

    Cookies are persisted between runs so restarting the server does not re-authenticate.
    Requests are serialised with a small delay - we are a personal client hitting a
    university server, not a crawler.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._authenticated = False
        # Deliberately no X-Requested-With here: MCV answers the OAuth bootstrap with a
        # bare 401 when it sees that header, instead of redirecting to the login form.
        # It is added per-request for the AJAX endpoints only.
        self._http = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en,th;q=0.9",
            },
        )
        self._load_cookies()

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MCVClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ cookie jar io

    def _load_cookies(self) -> None:
        path = self._config.cookie_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("could not read cookie jar, starting a fresh session")
            return
        for name, value in data.items():
            self._http.cookies.set(name, value, domain="www.mycourseville.com")

    def _save_cookies(self) -> None:
        try:
            self._config.cookie_path.write_text(
                json.dumps(dict(self._http.cookies)), "utf-8"
            )
        except OSError:
            log.warning("could not persist cookie jar")

    # ------------------------------------------------------------- transport

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)
        self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        with self._lock:
            self._throttle()
            try:
                return self._http.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise MCVError(f"request to {url} failed: {exc}") from exc

    # ------------------------------------------------------------------ login

    def login(self, force: bool = False) -> None:
        """Authenticate via Chula SSO.

        Reuses the persisted cookie jar unless `force` is set or the session is stale.
        """
        with self._lock:
            if not force and self._probe_session():
                self._authenticated = True
                return

            if not self._config.has_credentials:
                raise NotLoggedIn(
                    "MCV_USERNAME / MCV_PASSWORD are not set. Copy .env.example to .env "
                    "and fill in your Chula SSO credentials."
                )

            self._http.cookies.clear()

            if self._config.login_method == "mcv":
                login_url, credentials = MCV_LOGIN, {
                    "loginfield": "name",
                    "name": self._config.username,
                }
            else:
                login_url, credentials = CHULA_LOGIN, {
                    "username": self._config.username,
                }

            # 1. Bootstrap the OAuth session; this redirects to the right login form and
            #    is what makes the form's CSRF token valid.
            page = self._request(
                "GET", oauth_bootstrap(self._config.login_method_page)
            )

            # 2. Grab the CSRF token. The bootstrap usually lands on the form already; if
            #    it did not, fetch the form directly.
            match = _TOKEN_RE.search(page.text)
            if not match:
                page = self._request("GET", login_url)
                match = _TOKEN_RE.search(page.text)
            if not match:
                raise MCVError(
                    f"could not find the CSRF _token on {login_url} - "
                    "MCV's login flow has probably changed"
                )

            # 3. Submit credentials.
            self._request(
                "POST",
                login_url,
                data={
                    "_token": match.group(1),
                    **credentials,
                    "password": self._config.password,
                    "remember": "on",
                },
                headers={"Referer": login_url},
            )

            # A 200 here means nothing - a rejected login also renders a 200 page. Only a
            # successful data call proves we are actually authenticated.
            if not self._probe_session():
                raise NotLoggedIn(
                    "login did not take effect - check MCV_USERNAME / MCV_PASSWORD "
                    "(and whether the account needs 2FA in a browser first)"
                )

            self._authenticated = True
            self._save_cookies()
            log.info("logged in to MyCourseVille as %s", self._config.username)

    def _probe_session(self) -> bool:
        """True if the current cookies can see enrolled (non-public) courses."""
        try:
            semesters = self.get_semesters(_skip_auth=True)
            if not semesters:
                return False
            payload = self._ajax_json(
                "cvhomepanel_get_filter",
                {"yearsem": semesters[0], "role": "all", "type": "course"},
                _skip_auth=True,
            )
        except MCVError:
            return False

        courses = payload.get("data") or []
        return any(
            not str(c.get("course_no", "")).upper().startswith(_PUBLIC_COURSE_PREFIX)
            for c in courses
        )

    def ensure_login(self) -> None:
        if not self._authenticated:
            self.login()

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    # ------------------------------------------------------------ ajax helpers

    def _ajax_raw(
        self, command: str, data: dict[str, Any] | None = None, _skip_auth: bool = False
    ) -> httpx.Response:
        if not _skip_auth:
            self.ensure_login()
        return self._request(
            "POST",
            ajax(command),
            data=data or {},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": HOME},
        )

    def _ajax_json(
        self, command: str, data: dict[str, Any] | None = None, _skip_auth: bool = False
    ) -> dict[str, Any]:
        response = self._ajax_raw(command, data, _skip_auth=_skip_auth)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MCVError(f"ajax/{command} did not return JSON") from exc

        if isinstance(payload, dict) and payload.get("msg", "").startswith("Invalid command"):
            raise MCVError(f"MCV rejected ajax command {command!r}")
        return payload if isinstance(payload, dict) else {"data": payload}

    def ajax_html(
        self, command: str, data: dict[str, Any] | None = None
    ) -> str:
        """Run an AJAX command that answers with HTML (directly or wrapped in JSON)."""
        response = self._ajax_raw(command, data)
        text = response.text
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                payload = response.json()
            except ValueError:
                return text
            if isinstance(payload, dict):
                for key in ("html", "content", "data"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        return value
        return text

    # ------------------------------------------------------------------ reads

    def get_semesters(self, _skip_auth: bool = False) -> list[str]:
        """Year/semester codes, current one first (e.g. ['2026/1', '2027/2', '2027/1', …]).

        Document order is *not* useful here: the dropdown lists future semesters above the
        live one, so options[0] would be an empty semester. MCV marks the live semester
        with `data-value` on the <select>, so that goes to the front.
        """
        if not _skip_auth:
            self.ensure_login()
        html = self._request("GET", HOME).text
        block = _YEARSEM_BLOCK_RE.search(html)
        if not block:
            return []

        options = _OPTION_VALUE_RE.findall(block.group(0))
        current = _CURRENT_YEARSEM_RE.search(block.group(0))
        if current and current.group(1) in options:
            value = current.group(1)
            options = [value] + [o for o in options if o != value]
        return options

    def get_courses(self, yearsem: str) -> list[dict[str, Any]]:
        """Structured course list for one semester. This endpoint returns real JSON."""
        payload = self._ajax_json(
            "cvhomepanel_get_filter",
            {"yearsem": yearsem, "role": "all", "type": "course"},
        )
        if not payload.get("status"):
            return []
        return list(payload.get("data") or [])

    def get_active_panel_html(self) -> str:
        """The dashboard's 'active assignments' panel (fuzzy 'dues in ...' text)."""
        return self.ajax_html("getactivepanelcontent")

    def get_course_page(self, cv_cid: str | int, sub_page: str | None = None) -> str:
        """A course page's HTML.

        MCV's tabs are plain URL path segments, not an AJAX parameter: the bare course URL
        is the materials view, `/assignment` is the assignment list, and scores live under
        `/portfolio-<student id>`. All are server-rendered, so a GET is enough.
        """
        self.ensure_login()
        url = f"{BASE}/?q=courseville/course/{cv_cid}"
        if sub_page:
            url = f"{url}/{sub_page}"
        return self._request("GET", url).text

    def get_portfolio_page(self, cv_cid: str | int) -> str:
        """The student's own portfolio page for a course - this is where scores live."""
        return self.get_course_page(cv_cid, f"portfolio-{self._config.username}")

    def get_announcement_page(self, cv_cid: str | int, content_id: str | int) -> str:
        """One announcement's own page, which carries its full text."""
        return self.get_course_page(cv_cid, f"view_content_node_{content_id}")

    def get_worksheet_page(self, cv_cid: str | int, item_id: str | int) -> str:
        """One assignment's worksheet, which carries its instruction text."""
        self.ensure_login()
        return self._request(
            "GET", f"{BASE}/?q=courseville/worksheet/{cv_cid}/{item_id}"
        ).text

    def download(self, url: str, dest: Path) -> Path:
        """Download a course material to `dest`."""
        self.ensure_login()
        if url.startswith("/"):
            url = BASE + url
        response = self._request("GET", url)
        if response.status_code >= 400:
            raise MCVError(f"download failed ({response.status_code}) for {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest
