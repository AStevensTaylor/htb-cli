"""Machine lifecycle and discovery commands."""

from __future__ import annotations

from .. import api, catalog, config, hosts, proxy, ui, vpn
from . import common

BUSY_HINTS = ("you must stop", "already running", "active machine")


# --- discovery --------------------------------------------------------------

def _filter(machines, args):
    out = []
    for m in machines:
        if args.os and (m.get("os") or "").lower() != args.os.lower():
            continue
        if args.difficulty and (m.get("difficulty") or "").lower() != args.difficulty.lower():
            continue
        if args.state != "all" and m.get("state") != args.state:
            continue
        if args.todo and not m.get("todo"):
            continue
        if args.free and not m.get("free"):
            continue
        if args.owned is True and not m.get("root_owned"):
            continue
        if args.owned is False and m.get("root_owned"):
            continue
        out.append(m)
    return out


def _rows(machines):
    rows = []
    for m in machines:
        rows.append([
            ui.c(m.get("id"), "grey"),
            ui.c(m.get("name"), "bold"),
            f"{ui.os_icon(m.get('os'))} {m.get('os') or ''}".strip(),
            ui.difficulty(m.get("difficulty") or ""),
            m.get("points") or "",
            f"{ui.owned(m.get('user_owned'))}{ui.owned(m.get('root_owned'))}",
            (m.get("release") or "")[:10],
            ui.c(m.get("state") or "", "grey"),
        ])
    return rows


def search(args):
    client = common.client(args)
    machines = catalog.load(client, force=args.refresh)
    if args.query:
        machines = catalog.search_local(machines, " ".join(args.query))
    machines = _filter(machines, args)

    if args.sort == "name":
        machines.sort(key=lambda m: (m.get("name") or "").lower())
    elif args.sort == "difficulty":
        order = ["very easy", "easy", "medium", "hard", "insane"]
        machines.sort(key=lambda m: order.index(m["difficulty"].lower())
                      if (m.get("difficulty") or "").lower() in order else 99)
    elif args.sort == "release":
        machines.sort(key=lambda m: m.get("release") or "", reverse=True)

    shown = machines[: args.limit] if args.limit else machines
    ui.emit(shown, lambda: (
        ui.table(["ID", "NAME", "OS", "DIFFICULTY", "PTS", "OWN", "RELEASED", "STATE"],
                 _rows(shown), aligns=["r", "l", "l", "l", "r", "l", "l", "l"]),
        len(machines) > len(shown) and ui.info(
            f"{len(machines) - len(shown)} more; use --limit 0 to see everything"),
    ))


def info(args):
    client = common.client(args)
    profile = catalog.resolve(client, args.machine, assume_yes=args.yes)
    payload = dict(profile)
    try:
        payload["tags"] = client.machine_tags(profile["id"]).get("info", [])
    except api.ApiError:
        payload["tags"] = []

    def render():
        maker = profile.get("maker") or {}
        maker2 = profile.get("maker2") or {}
        authors = ", ".join(m.get("name") for m in (maker, maker2) if m and m.get("name"))
        tags = ", ".join(sorted({(t.get("name") or "")
                                 for t in payload["tags"] if isinstance(t, dict)}))
        print()
        print(f"  {ui.c(profile.get('name'), 'bold', 'cyan')}  "
              f"{ui.os_icon(profile.get('os'))} {profile.get('os') or ''}  "
              f"{ui.difficulty(profile.get('difficultyText') or '')}")
        print()
        ui.kv([
            ("id", profile.get("id")),
            ("points", profile.get("points")),
            ("rating", f"{profile.get('stars', '?')} ★  "
                       f"({profile.get('reviews_count', 0)} reviews)"),
            ("released", str(profile.get("release") or "")[:10]),
            ("state", "retired" if profile.get("retired") else
                      ("active" if profile.get("active") else "unreleased")),
            ("free", "yes" if profile.get("free") else "vip only" if profile.get("retired") else "yes"),
            ("owns", f"user {profile.get('user_owns_count', 0)} / "
                     f"root {profile.get('root_owns_count', 0)}"),
            ("your progress", f"user {ui.owned(profile.get('authUserInUserOwns'))}  "
                              f"root {ui.owned(profile.get('authUserInRootOwns'))}"),
            ("authors", authors),
            ("tags", tags),
            ("ip", ui.c(profile.get("ip"), "cyan") if profile.get("ip") else None),
            ("url", f"https://app.hackthebox.com/machines/{profile.get('id')}"),
        ], indent=2)
        print()

    ui.emit(payload, render)


# --- lifecycle --------------------------------------------------------------

def _spawn(client, profile, kind, assume_yes=False):
    """Ask HTB to start the machine; returns the API message."""
    machine_id = profile["id"]
    active = client.machine_active()
    if active and active.get("id") == machine_id:
        return "Machine is already running."
    if active and active.get("id") != machine_id:
        name = active.get("name", "another machine")
        if not ui.confirm(f"{name} is running. Stop it and start "
                          f"{profile.get('name')}?", assume_yes=assume_yes):
            ui.die("Aborted.")
        client.terminate(active["id"])
        with ui.Spinner(f"Stopping {name}…"):
            import time
            for _ in range(20):
                time.sleep(3)
                if not client.machine_active():
                    break

    if kind == "release":
        response = client.arena_start()
    else:
        response = client.spawn(machine_id)

    message = api.message_of(response, "Spawn requested.")
    if any(hint in message.lower() for hint in BUSY_HINTS):
        ui.die(message)
    return message


def start(args):
    client = common.client(args)
    profile = catalog.resolve(client, args.machine, assume_yes=args.yes)
    kind = catalog.machine_kind(client, profile)

    if not args.no_vpn:
        common.ensure_vpn(args, client, product=catalog.vpn_product(kind))

    message = _spawn(client, profile, kind, assume_yes=args.yes)
    ui.info(message)

    if not args.no_vpn:
        common.ensure_vpn_server(args, client,
                                 common.machine_vpn_server(client, profile, kind),
                                 product=catalog.vpn_product(kind))

    ip = common.wait_for_ip(client, profile, kind)
    ns = common.ns_for(args)
    if ip and not args.no_hosts and config.get("manage_hosts"):
        hosts.update(ip, profile["name"], ns=ns if common.vpn_mode(args) == "netns" else None)

    payload = {"machine": profile.get("name"), "id": profile.get("id"),
               "ip": ip, "kind": kind, "message": message,
               "proxy": proxy.read_meta(ns) or None}

    def render():
        print()
        ui.success(f"{ui.c(profile['name'], 'bold')} is up")
        ui.kv([
            ("target", ui.c(ip or "pending", "cyan", "bold")),
            ("hostname", f"{profile['name'].lower()}.htb"),
            ("os", profile.get("os")),
            ("difficulty", ui.difficulty(profile.get("difficultyText") or "")),
        ], indent=2)
        if common.vpn_mode(args) == "netns" and not args.no_vpn:
            print()
            ui.info(f"Only shells inside the namespace can reach it: "
                    f"{ui.c('htb shell', 'bold')}")
            where = proxy.endpoint(ns)
            if where:
                ui.info(f"Host-side tools (Burp, ZAP, curl) can use the lab proxy at "
                        f"{ui.c(f'{where[0]}:{where[1]}', 'bold')}")
        print()

    ui.emit(payload, render)

    if args.shell and ip:
        from . import net
        net.enter_shell(args, profile, ip)


def stop(args):
    client = common.client(args)
    profile = common.machine_arg(args, client, required=False)
    if not profile:
        ui.info("Nothing is running.")
        return
    response = client.terminate(profile["id"])
    ui.emit(response, lambda: ui.success(
        api.message_of(response, f"Stopping {profile.get('name')}.")))
    if config.get("manage_hosts"):
        hosts.clear(ns=common.ns_for(args) if common.vpn_mode(args) == "netns" else None)
    if args.vpn_down:
        vpn.down(common.ns_for(args))


def reset(args):
    client = common.client(args)
    profile = common.machine_arg(args, client)
    response = client.reset(profile["id"])
    ui.emit(response, lambda: ui.success(
        api.message_of(response, f"Reset requested for {profile.get('name')}.")))


def extend(args):
    client = common.client(args)
    profile = common.machine_arg(args, client)
    response = client.extend(profile["id"])
    ui.emit(response, lambda: ui.success(api.message_of(response, "Extended.")))


def active(args):
    client = common.client(args)
    info_ = client.machine_active()
    if not info_:
        season = None
        try:
            season = client.season_machine_active()
        except api.ApiError:
            pass
        info_ = season
    if not info_:
        ui.emit(None, lambda: ui.info("No machine is running."))
        return

    def render():
        ui.kv([
            ("name", ui.c(info_.get("name"), "bold")),
            ("id", info_.get("id")),
            ("ip", ui.c(info_.get("ip") or "pending…", "cyan")),
            ("type", info_.get("type")),
            ("expires", ui.rel_time(info_.get("expires_at"))),
            ("lab server", info_.get("lab_server")),
        ])
    ui.emit(info_, render)


def ip(args):
    """Print just the target IP - handy for `nmap $(htb ip)`."""
    client = common.client(args)
    info_ = client.machine_active()
    if not info_ or not info_.get("ip"):
        try:
            season = client.season_machine_active() or {}
        except api.ApiError:
            season = {}
        info_ = season if season.get("ip") else info_
    if not info_ or not info_.get("ip"):
        ui.die("No running machine with an IP yet.")
    print(info_["ip"])


def todo(args):
    client = common.client(args)
    profile = catalog.resolve(client, args.machine, assume_yes=args.yes)
    response = client.todo_update("machine", profile["id"])
    ui.emit(response, lambda: ui.success(
        api.message_of(response, f"Toggled to-do for {profile.get('name')}.")))
