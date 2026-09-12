"""login / logout / whoami / status."""

from __future__ import annotations

from .. import api, config, proxy, ui, vpn
from . import common


def login(args):
    token = args.token or ui.secret("Paste your HTB App Token")
    token = token.strip()
    if not token:
        ui.die("No token given.")
    probe = api.Client(token, debug=args.debug)
    try:
        info = probe.user_info()
    except api.ApiError as exc:
        ui.die(f"Token rejected: {exc.message}")
    config.set_token(token)
    ui.success(f"Logged in as {ui.c(info.get('name', '?'), 'bold')} "
               f"({probe.subscription()}). Token saved to {config.TOKEN_FILE}")


def logout(args):
    if config.clear_token():
        ui.success("Token removed.")
    else:
        ui.warn("No stored token.")


def whoami(args):
    client = common.client(args)
    info = client.user_info()
    sub = client.subscription()
    ui.emit(info, lambda: ui.kv([
        ("user", ui.c(info.get("name"), "bold")),
        ("id", info.get("id")),
        ("plan", sub),
        ("team", (info.get("team") or {}).get("name")),
        ("rank", info.get("rank")),
        ("points", info.get("points")),
        ("server", (info.get("server") or {}).get("friendly_name")
         if isinstance(info.get("server"), dict) else info.get("server_name")),
        ("profile", f"https://app.hackthebox.com/users/{info.get('id')}"),
    ]))


def status(args):
    client = common.client(args)
    ns = common.ns_for(args)
    mode = common.vpn_mode(args)

    info, sub = client.user_info(), None
    try:
        sub = client.subscription()
    except api.ApiError:
        pass

    try:
        active = client.machine_active()
    except api.ApiError:
        active = None

    local_up = vpn.is_up(ns, mode)
    meta = vpn.read_meta(ns) if local_up else {}
    try:
        remote = client.connection_status()
    except api.ApiError:
        remote = []

    proxy_state = proxy.status(ns) if mode == "netns" else {"running": False}
    payload = {
        "user": {"name": info.get("name"), "id": info.get("id"), "plan": sub},
        "vpn": {"local": meta if local_up else None, "htb": remote},
        "proxy": proxy_state,
        "active_machine": active,
    }

    def render():
        ui.kv([("user", f"{ui.c(info.get('name'), 'bold')}  {ui.c(sub or '', 'grey')}")])
        print()
        ui.rule("vpn")
        if local_up:
            scope = (f"namespace {ui.c(ns, 'bold')}" if mode == "netns"
                     else ui.c("system-wide", "yellow"))
            ui.kv([
                ("state", ui.c("connected", "green")),
                ("scope", scope),
                ("server", meta.get("name")),
                ("product", meta.get("product")),
                ("tun ip", meta.get("ip") or vpn.tun_ip(ns)),
                ("internet", "yes" if meta.get("internet") else "lab only"),
                ("lab proxy", (f"{proxy_state['host']}:{proxy_state['port']}"
                               + ("" if proxy_state.get("reachable") else " (not answering)"))
                 if proxy_state.get("running") else "off"),
            ])
        else:
            ui.kv([("state", ui.c("not connected locally", "grey"))])
        for conn in remote or []:
            server = conn.get("server") or {}
            ui.kv([("htb sees", f"{server.get('friendly_name', '?')} "
                    f"({conn.get('type', '?')})")])
        print()
        ui.rule("active machine")
        if active:
            ui.kv([
                ("name", ui.c(active.get("name"), "bold")),
                ("ip", ui.c(active.get("ip") or "pending…", "cyan")),
                ("type", active.get("type")),
                ("expires", ui.rel_time(active.get("expires_at"))),
            ])
        else:
            ui.kv([("state", ui.c("nothing running", "grey"))])

    ui.emit(payload, render)
