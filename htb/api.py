"""Thin client for the (undocumented) Hack The Box v4 API.

All endpoints live here so they are easy to fix in one place when HTB moves
things around.  Nothing outside this module builds a URL.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

BASE = "https://labs.hackthebox.com/api/v4"
USER_AGENT = "htb-cli/1.0 (+https://github.com/)"
TOKEN_URL = "https://app.hackthebox.com/profile/settings"


class ApiError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


class AuthError(ApiError):
    pass


class RateLimited(ApiError):
    def __init__(self, message, retry_after=60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class Client:
    def __init__(self, token: str | None = None, debug: bool = False, timeout: int = 30):
        self.token = token or config.get_token()
        self.debug = debug
        self.timeout = timeout

    # -- plumbing ------------------------------------------------------------

    def _require_token(self):
        if not self.token:
            raise AuthError(
                "No API token. Run `htb login` (create an App Token at "
                f"{TOKEN_URL})."
            )

    def request(self, method, path, *, params=None, data=None, raw=False, retries=2):
        """Perform an API call. `data` is sent as JSON. Returns parsed JSON."""
        self._require_token()
        url = path if path.startswith("http") else BASE + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)

        body = None
        headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/plain, */*",
        }
        if method.upper() in ("POST", "PUT", "PATCH"):
            body = json.dumps(data if data is not None else {}).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        if self.debug:
            from . import ui
            ui.warn(f"{method.upper()} {url}" + (f" {body.decode()}" if body else ""))

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload
                return self._decode(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            text = payload.decode("utf-8", "replace")
            if exc.code in (401, 403) or "/login" in (exc.headers.get("Location") or ""):
                raise AuthError(
                    self._message(text) or "Token rejected (expired or missing scope). "
                    "Run `htb login` with a fresh App Token.",
                    status=exc.code, body=text,
                ) from None
            if exc.code == 429:
                wait = int(exc.headers.get("Retry-After") or 60)
                if retries > 0:
                    from . import ui
                    ui.warn(f"Rate limited by HTB, retrying in {wait}s…")
                    time.sleep(wait)
                    return self.request(method, path, params=params, data=data,
                                        raw=raw, retries=retries - 1)
                raise RateLimited("Rate limited by HTB. Try again shortly.",
                                  retry_after=wait, status=429, body=text) from None
            if exc.code >= 500 and retries > 0:
                time.sleep(2)
                return self.request(method, path, params=params, data=data,
                                    raw=raw, retries=retries - 1)
            raise ApiError(self._message(text) or f"HTTP {exc.code} for {path}",
                           status=exc.code, body=text) from None
        except urllib.error.URLError as exc:
            raise ApiError(f"Network error talking to HTB: {exc.reason}") from None

    @staticmethod
    def _decode(payload: bytes):
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except ValueError:
            return {"raw": payload.decode("utf-8", "replace")}

    @staticmethod
    def _message(text: str) -> str | None:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        for key in ("message", "error", "detail"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str):
                return value
        return None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.request("POST", path, data=data or {}, **kw)

    # -- user ----------------------------------------------------------------

    def user_info(self) -> dict:
        return self.get("/user/info").get("info", {})

    def user_settings(self) -> dict:
        return self.get("/user/settings")

    def profile_basic(self, user_id) -> dict:
        return self.get(f"/user/profile/basic/{user_id}").get("profile", {})

    def subscription(self) -> str:
        info = self.user_info()
        if info.get("canAccessVIP"):
            return "vip+" if info.get("isDedicatedVip") else "vip"
        return "free"

    # -- machines ------------------------------------------------------------

    def machine_profile(self, slug) -> dict:
        return self.get(f"/machine/profile/{urllib.parse.quote(str(slug))}").get("info", {})

    def machine_active(self) -> dict | None:
        return self.get("/machine/active").get("info") or None

    def machine_recommended(self) -> dict:
        return self.get("/machine/recommended")

    def machines_page(self, page=1, per_page=100, retired=False) -> dict:
        path = "/machine/list/retired/paginated" if retired else "/machine/paginated"
        return self.get(path, params={"per_page": per_page, "page": page})

    def machines_unreleased(self) -> list:
        data = self.get("/machine/unreleased")
        return data.get("data", data if isinstance(data, list) else [])

    def machine_tags(self, machine_id) -> dict:
        return self.get(f"/machine/tags/{machine_id}")

    def machine_activity(self, machine_id) -> dict:
        return self.get(f"/machine/activity/{machine_id}")

    def machine_changelog(self, machine_id) -> dict:
        return self.get(f"/machine/changelog/{machine_id}")

    def machine_writeup(self, machine_id) -> bytes:
        return self.get(f"/machine/writeup/{machine_id}", raw=True)

    def todo_update(self, product, product_id) -> dict:
        return self.post(f"/{product}/todo/update/{product_id}")

    # -- vm lifecycle --------------------------------------------------------

    def spawn(self, machine_id) -> dict:
        """Start a machine. One route for every tier — free accounts land on a
        shared lab server, VIP/VIP+ get a private instance."""
        return self.post("/vm/spawn", {"machine_id": int(machine_id)})

    def terminate(self, machine_id) -> dict:
        return self.post("/vm/terminate", {"machine_id": int(machine_id)})

    def reset(self, machine_id) -> dict:
        return self.post("/vm/reset", {"machine_id": int(machine_id)})

    def extend(self, machine_id) -> dict:
        return self.post("/vm/extend", {"machine_id": int(machine_id)})

    def arena_start(self) -> dict:
        return self.post("/arena/start")

    def arena_stop(self) -> dict:
        return self.post("/arena/stop")

    def arena_reset(self) -> dict:
        return self.post("/arena/reset")

    # -- flags ---------------------------------------------------------------

    def own_machine(self, machine_id, flag, difficulty=None) -> dict:
        payload = {"id": int(machine_id), "flag": flag}
        if difficulty:
            payload["difficulty"] = int(difficulty) * 10
        return self.post("/machine/own", payload)

    def own_arena(self, machine_id, flag, difficulty=None) -> dict:
        payload = {"id": int(machine_id), "flag": flag}
        if difficulty:
            payload["difficulty"] = int(difficulty) * 10
        return self.post("/arena/own", payload)

    def own_challenge(self, challenge_id, flag, difficulty=5) -> dict:
        return self.post("/challenge/own", {
            "challenge_id": int(challenge_id),
            "flag": flag,
            "difficulty": int(difficulty) * 10,
        })

    def own_fortress(self, fortress_id, flag) -> dict:
        return self.post(f"/fortress/{int(fortress_id)}/flag", {"flag": flag})

    def own_prolab(self, prolab_id, flag) -> dict:
        return self.post(f"/prolab/{int(prolab_id)}/flag", {"flag": flag})

    def own_sherlock(self, sherlock_id, task_id, flag) -> dict:
        return self.post(f"/sherlocks/{int(sherlock_id)}/tasks/{int(task_id)}/flag",
                         {"flag": flag})

    def achievement_link(self, user_id, machine_id) -> str:
        return f"https://labs.hackthebox.com/achievement/machine/{user_id}/{machine_id}"

    # -- search --------------------------------------------------------------

    def search(self, query, tags=None) -> dict:
        params = {"query": query}
        if tags:
            params["tags[]"] = tags
        return self.get("/search/fetch", params=params)

    # -- challenges ----------------------------------------------------------

    def challenges(self, state="active") -> list:
        data = self.get("/challenges", params={"state": state})
        return data.get("data", []) if isinstance(data, dict) else data

    def challenge_info(self, slug) -> dict:
        data = self.get(f"/challenge/info/{urllib.parse.quote(str(slug))}")
        return data.get("challenge", data)

    def challenge_categories(self) -> list:
        data = self.get("/challenge/categories/list")
        return data.get("info", data) if isinstance(data, dict) else data

    def challenge_start(self, challenge_id) -> dict:
        return self.post("/challenge/start", {"challenge_id": int(challenge_id)})

    def challenge_stop(self, challenge_id) -> dict:
        return self.post("/challenge/stop", {"challenge_id": int(challenge_id)})

    def challenge_download(self, challenge_id) -> bytes:
        return self.get(f"/challenge/download/{int(challenge_id)}", raw=True)

    # -- season --------------------------------------------------------------

    def season_list(self) -> list:
        data = self.get("/season/list")
        return data.get("data", []) if isinstance(data, dict) else data

    def season_machine_active(self) -> dict | None:
        return self.get("/season/machine/active").get("data") or None

    def season_rank(self, season_id) -> dict:
        return self.get(f"/season/user/rank/{season_id}").get("data", {})

    # -- vpn -----------------------------------------------------------------

    def connection_status(self) -> list:
        data = self.get("/connection/status")
        return data if isinstance(data, list) else data.get("data", [])

    def vpn_servers(self, product="labs") -> dict:
        return self.get("/connections/servers", params={"product": product})

    def vpn_switch(self, vpn_id) -> dict:
        return self.post(f"/connections/servers/switch/{int(vpn_id)}")

    def ovpn_file(self, vpn_id, tcp=False) -> bytes:
        suffix = "/0/1" if tcp else "/0"
        return self.get(f"/access/ovpnfile/{int(vpn_id)}{suffix}", raw=True)


# --- shared helpers ---------------------------------------------------------

def message_of(response, default="") -> str:
    if isinstance(response, dict):
        for key in ("message", "status", "info"):
            value = response.get(key)
            if isinstance(value, str):
                return value
    return default
