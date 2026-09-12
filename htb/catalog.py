"""Machine catalogue: caching, searching and name -> machine resolution."""

from __future__ import annotations

import json
import time

from . import api, config, ui

CACHE_FILE = lambda: config.CACHE_DIR / "machines.json"  # noqa: E731


def _normalise(raw: dict, state: str) -> dict:
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "os": raw.get("os"),
        "difficulty": raw.get("difficultyText") or raw.get("difficulty_text") or "",
        "points": raw.get("points"),
        "stars": raw.get("star") or raw.get("stars"),
        "release": (raw.get("release") or raw.get("release_date") or "")[:10],
        "state": state,
        "free": bool(raw.get("free")),
        "user_owned": bool(raw.get("authUserInUserOwns")),
        "root_owned": bool(raw.get("authUserInRootOwns")),
        "todo": bool(raw.get("isTodo")),
        "user_owns": raw.get("user_owns_count"),
        "root_owns": raw.get("root_owns_count"),
        "active": bool(raw.get("active")),
        "sp": bool(raw.get("sp_flag")),
    }


def _fetch_paged(client: api.Client, retired: bool, state: str, limit_pages: int = 40) -> list:
    machines, page = [], 1
    while page <= limit_pages:
        data = client.machines_page(page=page, retired=retired)
        items = data.get("data") if isinstance(data, dict) else data
        if not items:
            break
        machines.extend(_normalise(m, state) for m in items)
        meta = (data.get("meta") or {}) if isinstance(data, dict) else {}
        last = meta.get("last_page")
        if last and page >= last:
            break
        if not last and len(items) < 100:
            break
        page += 1
    return machines


def refresh(client: api.Client, include_retired: bool = True,
            include_unreleased: bool = True) -> list:
    """Pull the full machine catalogue from HTB and cache it."""
    machines = []
    with ui.Spinner("Fetching active machines…") as spin:
        machines += _fetch_paged(client, retired=False, state="active")
        if include_retired:
            spin.update("Fetching retired machines…")
            try:
                machines += _fetch_paged(client, retired=True, state="retired")
            except api.ApiError as exc:
                ui.warn(f"Could not list retired machines: {exc.message}")
        if include_unreleased:
            spin.update("Fetching unreleased machines…")
            try:
                machines += [_normalise(m, "unreleased") for m in client.machines_unreleased()]
            except api.ApiError:
                pass

    seen, unique = set(), []
    for machine in machines:
        if machine["id"] in seen or machine["id"] is None:
            continue
        seen.add(machine["id"])
        unique.append(machine)

    config.ensure_dirs()
    CACHE_FILE().write_text(json.dumps({"fetched": time.time(), "machines": unique}))
    return unique


def load(client: api.Client, max_age: int | None = None, force: bool = False) -> list:
    """Cached catalogue, refreshed when stale."""
    max_age = config.get("cache_ttl") if max_age is None else max_age
    if not force:
        try:
            blob = json.loads(CACHE_FILE().read_text())
            if time.time() - blob.get("fetched", 0) < max_age and blob.get("machines"):
                return blob["machines"]
        except (OSError, ValueError):
            pass
    return refresh(client)


def cached_only() -> list:
    try:
        return json.loads(CACHE_FILE().read_text()).get("machines", [])
    except (OSError, ValueError):
        return []


# --- search -----------------------------------------------------------------

def score(machine: dict, query: str) -> int:
    """Crude relevance ranking: exact > prefix > substring > subsequence."""
    name = (machine.get("name") or "").lower()
    query = query.lower()
    if not name:
        return 0
    if name == query:
        return 100
    if name.startswith(query):
        return 80 - len(name)
    if query in name:
        return 60 - len(name)
    # subsequence match ("htbx" -> "HTB eXample")
    it = iter(name)
    if all(ch in it for ch in query):
        return 30 - len(name)
    return 0


def search_local(machines: list, query: str) -> list:
    scored = [(score(m, query), m) for m in machines]
    hits = [m for s, m in sorted(scored, key=lambda p: -p[0]) if s > 0]
    return hits


def search_remote(client: api.Client, query: str) -> list:
    """HTB's own search endpoint; shapes vary (list or dict of dicts)."""
    try:
        data = client.search(query, tags=["machines"])
    except api.ApiError:
        return []
    machines = data.get("machines") if isinstance(data, dict) else None
    if isinstance(machines, dict):
        machines = list(machines.values())
    if not isinstance(machines, list):
        return []
    return [{"id": int(m["id"]), "name": m.get("value") or m.get("name")}
            for m in machines if m.get("id")]


def resolve(client: api.Client, query, assume_yes: bool = False) -> dict:
    """Turn a machine id/name/fragment into a full machine profile."""
    query = str(query).strip()
    if not query:
        ui.die("No machine given.")

    if query.isdigit():
        return client.machine_profile(int(query))

    # HTB accepts the exact slug on the profile endpoint - fastest path.
    try:
        profile = client.machine_profile(query)
        if profile.get("id"):
            return profile
    except api.ApiError:
        pass

    candidates = search_local(cached_only(), query)[:10]
    if not candidates:
        candidates = search_remote(client, query)[:10]
    if not candidates:
        candidates = search_local(load(client), query)[:10]
    if not candidates:
        ui.die(f"No machine matches {query!r}.")

    def render(m):
        bits = [ui.c(m["name"], "bold")]
        if m.get("os"):
            bits.append(m["os"])
        if m.get("difficulty"):
            bits.append(ui.difficulty(m["difficulty"]))
        if m.get("state"):
            bits.append(ui.c(m["state"], "grey"))
        return "  ".join(bits)

    if len(candidates) > 1 and not assume_yes:
        picked = ui.choose(f"Machines matching {query!r}:", candidates, render)
    else:
        picked = candidates[0]
        ui.info(f"Using machine {ui.c(picked['name'], 'bold')}")
    if not picked:
        ui.die("Nothing selected.")
    return client.machine_profile(picked["id"])


def machine_kind(client: api.Client, profile: dict) -> str:
    """'release' (season/arena), 'sp', 'active' or 'retired'."""
    if profile.get("sp_flag"):
        return "sp"
    try:
        season = client.season_machine_active()
        if season and season.get("id") == profile.get("id"):
            return "release"
    except api.ApiError:
        pass
    if profile.get("retired"):
        return "retired"
    return "active"


def vpn_product(kind: str) -> str:
    return {"release": "competitive", "sp": "starting_point"}.get(kind, "labs")
