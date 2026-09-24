"""Shared helpers for command implementations."""

from __future__ import annotations

import os

from .. import api, catalog, config, proxy, ui, vpn


def client(args) -> api.Client:
    token = config.get_token()
    if not token:
        ui.die("Not logged in. Run `htb login` first "
               f"(create an App Token at {api.TOKEN_URL}).")
    return api.Client(token, debug=getattr(args, "debug", False))


def ns_for(args) -> str:
    """Namespace name for this invocation ('global' means system-wide)."""
    if getattr(args, "global_vpn", False):
        return "global"
    return getattr(args, "netns", None) or config.get("netns")


def vpn_mode(args) -> str:
    return "global" if getattr(args, "global_vpn", False) else "netns"


def academy(args) -> bool:
    """Did the user ask for the HTB Academy VPN (or hand us its .ovpn)?"""
    return bool(getattr(args, "academy", False) or getattr(args, "ovpn", None))


def vpn_product(args) -> str:
    """Product for `vpn up`/`shell`: --academy, --product, else the config default.
    A given --ovpn file is imported as the Academy config on the way."""
    if getattr(args, "ovpn", None):
        from pathlib import Path
        dest = vpn.import_config("academy", Path(args.ovpn))
        ui.info(f"Imported Academy VPN config to {dest}")
    if academy(args):
        return "academy"
    return getattr(args, "product", None) or config.get("vpn_product")


def machine_arg(args, client_, required=True, allow_active=True):
    """Resolve the machine named on the command line, else the active one."""
    name = getattr(args, "machine", None)
    if name:
        return catalog.resolve(client_, name, assume_yes=getattr(args, "yes", False))
    if allow_active:
        active = client_.machine_active()
        if active and active.get("id"):
            return client_.machine_profile(active["id"])
    if required:
        ui.die("No machine given and none is running. Pass a machine name.")
    return None


def ensure_vpn(args, client_, product="labs"):
    """Bring the VPN up unless the user opted out. Returns metadata or None."""
    if getattr(args, "no_vpn", False):
        return None
    ns = ns_for(args)
    mode = vpn_mode(args)
    if mode == "global" and vpn.global_tun_present() and not vpn.is_up(ns, mode):
        ui.info("An HTB tunnel already appears to be up; leaving it alone.")
        return None
    meta = vpn.up(
        client_,
        ns=ns,
        product=product,
        tcp=getattr(args, "tcp", False) or config.get("vpn_protocol") == "tcp",
        internet=not getattr(args, "no_internet", False) and config.get("netns_internet"),
        veth=not getattr(args, "no_veth", False) and config.get("netns_veth"),
        mode=mode,
    )
    proxy.maybe_up(args, meta)
    return meta


def machine_vpn_server(client_, profile: dict, kind: str) -> int | None:
    """The lab server a spawned machine sits behind, if HTB tells us."""
    try:
        if kind == "release":
            data = client_.season_machine_active() or {}
        else:
            data = client_.machine_active() or {}
            if data.get("id") != profile.get("id"):
                return None
        return int(data.get("vpn_server_id") or 0) or None
    except (api.ApiError, TypeError, ValueError):
        return None


def ensure_vpn_server(args, client_, server_id, product="labs"):
    """Move the tunnel onto `server_id` when the machine lives somewhere else.

    Free machines are pinned to one lab server: spawning works from anywhere,
    but only that server can route to the target.  Does nothing when the
    tunnel is not ours to move (no VPN, or one we did not bring up).
    """
    if not server_id or getattr(args, "no_vpn", False):
        return None
    ns = ns_for(args)
    meta = vpn.read_meta(ns)
    current = meta.get("id")
    if current is None or int(current) == int(server_id):
        return meta or None

    ui.info(f"Target is on VPN server {server_id}; reconnecting from "
            f"{meta.get('name') or current}")
    try:
        client_.vpn_switch(server_id)
    except api.ApiError as exc:
        ui.warn(f"Could not switch VPN server: {exc.message}")
        ui.warn(f"The target will be unreachable until you run "
                f"`htb vpn switch {server_id}` and reconnect.")
        return meta
    vpn.down(ns, quiet=True)
    return ensure_vpn(args, client_, product=product)


def target_env(profile: dict, ip: str, ns: str) -> dict:
    env = {
        "HTB_NETNS": ns,
        "HTB_MACHINE": str(profile.get("name") or ""),
        "HTB_MACHINE_ID": str(profile.get("id") or ""),
        "HTB_TARGET": ip or "",
        "TERM": os.environ.get("TERM", "xterm-256color"),
    }
    return {k: v for k, v in env.items() if v}


def machine_ip(client_, profile: dict, kind: str) -> str | None:
    """Current IP of a spawned machine, if HTB has handed one out yet."""
    if kind == "release":
        data = client_.season_machine_active() or {}
        return data.get("ip") or None
    active = client_.machine_active()
    if active and active.get("id") == profile.get("id") and active.get("ip"):
        return active["ip"]
    fresh = client_.machine_profile(profile["id"])
    return fresh.get("ip") or None


def wait_for_ip(client_, profile: dict, kind: str, timeout: int = 300) -> str | None:
    import time
    with ui.Spinner(f"Waiting for {profile.get('name')} to boot…") as spin:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ip = machine_ip(client_, profile, kind)
            except api.ApiError as exc:
                spin.stop()
                ui.warn(f"Could not read machine state: {exc.message}")
                return None
            if ip:
                return ip
            time.sleep(3)
    ui.warn("Timed out waiting for an IP address; check `htb active`.")
    return None
