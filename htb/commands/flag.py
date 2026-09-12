"""Flag submission."""

from __future__ import annotations

import re

from .. import api, catalog, ui
from . import common

HEX32 = re.compile(r"^[0-9a-f]{32}$", re.I)


def _clean(flag: str) -> str:
    return flag.strip().strip("\"'").replace(" ", "")


def _read_flag(args) -> str:
    flag = args.flag or ui.secret("Flag")
    flag = _clean(flag)
    if not flag:
        ui.die("No flag given.")
    return flag


def resolve_challenge(client, query, assume_yes=False) -> dict:
    try:
        found = client.challenge_info(query)
        if found.get("id"):
            return found
    except api.ApiError:
        pass
    try:
        data = client.search(query, tags=["challenges"])
    except api.ApiError:
        data = {}
    items = data.get("challenges") if isinstance(data, dict) else None
    if isinstance(items, dict):
        items = list(items.values())
    items = [i for i in (items or []) if i.get("id")]
    if not items:
        ui.die(f"No challenge matches {query!r}.")
    picked = ui.choose("Challenges:", items, lambda c: c.get("value") or c.get("name"),
                       assume_yes=assume_yes)
    if not picked:
        ui.die("Nothing selected.")
    return {"id": int(picked["id"]), "name": picked.get("value") or picked.get("name")}


def submit(args):
    client = common.client(args)

    if args.challenge:
        challenge = resolve_challenge(client, args.challenge, args.yes)
        flag = _read_flag(args)
        _send(lambda: client.own_challenge(challenge["id"], flag, args.difficulty or 5),
              challenge.get("name") or args.challenge)
        return

    if args.fortress:
        flag = _read_flag(args)
        _send(lambda: client.own_fortress(args.fortress, flag), f"fortress {args.fortress}")
        return

    if args.prolab:
        flag = _read_flag(args)
        _send(lambda: client.own_prolab(args.prolab, flag), f"prolab {args.prolab}")
        return

    # machines (incl. the seasonal release arena)
    if args.arena:
        season = client.season_machine_active() or {}
        if not season.get("id"):
            ui.die("No active release-arena machine.")
        profile = {"id": season["id"], "name": season.get("name", "release arena")}
        kind = "release"
    else:
        profile = common.machine_arg(args, client)
        kind = catalog.machine_kind(client, profile)

    flag = _read_flag(args)
    if not HEX32.match(flag):
        ui.warn("That doesn't look like a 32-character machine flag - submitting anyway.")

    def call():
        if kind == "release":
            return client.own_arena(profile["id"], flag, args.difficulty)
        return client.own_machine(profile["id"], flag, args.difficulty)

    response = _send(call, profile.get("name"))
    if response is not None:
        try:
            user_id = client.user_info().get("id")
            ui.info(client.achievement_link(user_id, profile["id"]))
        except api.ApiError:
            pass


def _send(call, label):
    try:
        response = call()
    except api.ApiError as exc:
        message = exc.message or "Flag rejected."
        if "incorrect" in message.lower() or exc.status == 400:
            ui.error(f"{message}")
            raise SystemExit(2)
        ui.die(message)
    message = api.message_of(response, "Submitted.")
    ui.emit(response, lambda: ui.success(f"{ui.c(label, 'bold')}: {message}"))
    return response
