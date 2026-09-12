"""Challenges, season, config, raw API access and shell completion."""

from __future__ import annotations

import json
from pathlib import Path

from .. import api, catalog, config, ui
from . import common
from .flag import resolve_challenge


# --- challenges -------------------------------------------------------------

def challenge_list(args):
    client = common.client(args)
    items = client.challenges(state=args.state)
    if args.category:
        items = [c for c in items
                 if args.category.lower() in str(c.get("category_name")
                                                 or c.get("category") or "").lower()]
    if args.query:
        needle = " ".join(args.query).lower()
        items = [c for c in items if needle in str(c.get("name", "")).lower()]
    items = items[: args.limit] if args.limit else items
    rows = [[
        ui.c(c.get("id"), "grey"),
        ui.c(c.get("name"), "bold"),
        c.get("category_name") or c.get("category") or "",
        ui.difficulty(c.get("difficulty") or ""),
        c.get("static_points") or c.get("points") or "",
        ui.owned(c.get("authUserSolve") or c.get("solved")),
    ] for c in items]
    ui.emit(items, lambda: ui.table(["ID", "NAME", "CATEGORY", "DIFFICULTY", "PTS", "OWN"],
                                    rows, aligns=["r", "l", "l", "l", "r", "l"]))


def challenge_info(args):
    client = common.client(args)
    challenge = resolve_challenge(client, args.challenge, args.yes)
    if "description" not in challenge:
        challenge = client.challenge_info(challenge["id"])

    def render():
        print()
        print(f"  {ui.c(challenge.get('name'), 'bold', 'cyan')}  "
              f"{challenge.get('category_name', '')}  "
              f"{ui.difficulty(challenge.get('difficulty') or '')}")
        print()
        ui.kv([
            ("id", challenge.get("id")),
            ("points", challenge.get("points") or challenge.get("static_points")),
            ("solves", challenge.get("solves")),
            ("released", str(challenge.get("release_date") or "")[:10]),
            ("docker", "yes" if challenge.get("docker") else "no"),
            ("downloadable", "yes" if challenge.get("download") else "no"),
            ("solved", "yes" if challenge.get("authUserSolve") else "no"),
        ], indent=2)
        if challenge.get("description"):
            print()
            print("  " + str(challenge["description"]).strip().replace("\n", "\n  "))
        print()
    ui.emit(challenge, render)


def challenge_start(args):
    client = common.client(args)
    challenge = resolve_challenge(client, args.challenge, args.yes)
    response = client.challenge_start(challenge["id"])

    def render():
        ui.success(api.message_of(response, "Container starting."))
        target = response.get("ip") or response.get("host")
        port = response.get("port")
        if target:
            ui.kv([("target", ui.c(f"{target}:{port}" if port else target, "cyan"))], indent=2)
    ui.emit(response, render)


def challenge_stop(args):
    client = common.client(args)
    challenge = resolve_challenge(client, args.challenge, args.yes)
    response = client.challenge_stop(challenge["id"])
    ui.emit(response, lambda: ui.success(api.message_of(response, "Container stopped.")))


def challenge_download(args):
    client = common.client(args)
    challenge = resolve_challenge(client, args.challenge, args.yes)
    blob = client.challenge_download(challenge["id"])
    name = (challenge.get("name") or str(challenge["id"])).replace(" ", "_")
    dest = Path(args.output).expanduser() if args.output else Path.cwd() / f"{name}.zip"
    if dest.is_dir():
        dest = dest / f"{name}.zip"
    dest.write_bytes(blob)
    ui.emit({"file": str(dest), "bytes": len(blob)},
            lambda: ui.success(f"Saved {len(blob)} bytes to {dest}  "
                               f"(password: {ui.c('hackthebox', 'bold')})"))


# --- season -----------------------------------------------------------------

def season(args):
    client = common.client(args)
    seasons = client.season_list()
    current = next((s for s in seasons if s.get("active") or s.get("state") == "active"),
                   seasons[0] if seasons else None)
    machine = None
    try:
        machine = client.season_machine_active()
    except api.ApiError:
        pass
    rank = {}
    if current:
        try:
            rank = client.season_rank(current.get("id"))
        except api.ApiError:
            pass

    payload = {"season": current, "machine": machine, "rank": rank}

    def render():
        if current:
            ui.rule(str(current.get("name") or "season"))
            ui.kv([("ends", ui.rel_time(current.get("end_date"))),
                   ("tier", rank.get("current_tier") or rank.get("tier")),
                   ("rank", rank.get("rank")),
                   ("points", rank.get("total_points") or rank.get("points"))])
        if machine:
            print()
            ui.rule("this week's release")
            ui.kv([
                ("name", ui.c(machine.get("name"), "bold")),
                ("os", machine.get("os")),
                ("difficulty", ui.difficulty(machine.get("difficulty_text")
                                             or machine.get("difficultyText") or "")),
                ("ip", ui.c(machine.get("ip"), "cyan") if machine.get("ip") else None),
                ("state", "running" if machine.get("ip") else "not spawned"),
            ])
    ui.emit(payload, render)


# --- config -----------------------------------------------------------------

def config_cmd(args):
    if args.action == "list" or (args.action == "get" and not args.key):
        ui.emit(config.load(), lambda: ui.kv(sorted(config.load().items())))
        return
    if args.action == "get":
        value = config.get(args.key)
        ui.emit({args.key: value}, lambda: print(value))
        return
    if args.action == "set":
        if args.key not in config.DEFAULTS:
            ui.warn(f"Unknown key {args.key!r} (known: {', '.join(sorted(config.DEFAULTS))})")
        raw = args.value
        if raw.lower() in ("true", "false"):
            value = raw.lower() == "true"
        elif raw.isdigit():
            value = int(raw)
        else:
            value = raw
        config.set_(args.key, value)
        ui.success(f"{args.key} = {value}")


# --- raw --------------------------------------------------------------------

def raw(args):
    client = common.client(args)
    data = json.loads(args.data) if args.data else None
    path = args.path if args.path.startswith("/") else "/" + args.path
    response = client.request(args.method.upper(), path, data=data)
    ui.json_dump(response)


# --- refresh ----------------------------------------------------------------

def refresh(args):
    client = common.client(args)
    machines = catalog.refresh(client)
    ui.success(f"Cached {len(machines)} machines in {catalog.CACHE_FILE()}")


# --- completion -------------------------------------------------------------

BASH_COMPLETION = r"""# htb-cli bash completion - source this file or drop it in
# /usr/share/bash-completion/completions/htb
_htb_completions() {
  local cur prev commands
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  commands="login logout whoami status search info start stop reset extend active ip \
todo submit shell exec vpn proxy challenge season config raw refresh completion"
  case "$prev" in
    htb) COMPREPLY=($(compgen -W "$commands" -- "$cur")); return;;
    vpn) COMPREPLY=($(compgen -W "up down status servers switch config" -- "$cur")); return;;
    proxy) COMPREPLY=($(compgen -W "up down status url" -- "$cur")); return;;
    challenge) COMPREPLY=($(compgen -W "list info start stop download" -- "$cur")); return;;
    config) COMPREPLY=($(compgen -W "list get set" -- "$cur")); return;;
    start|info|stop|reset|extend|todo|shell)
      local cache="${XDG_CACHE_HOME:-$HOME/.cache}/htb-cli/machines.json"
      if [ -f "$cache" ]; then
        COMPREPLY=($(compgen -W "$(grep -o '"name": "[^"]*"' "$cache" | cut -d'"' -f4)" -- "$cur"))
      fi
      return;;
  esac
  COMPREPLY=($(compgen -W "$commands --json --yes --debug --help" -- "$cur"))
}
complete -F _htb_completions htb
"""

ZSH_COMPLETION = r"""#compdef htb
_htb() {
  local -a commands
  commands=(login logout whoami status search info start stop reset extend active ip
            todo submit shell exec vpn proxy challenge season config raw refresh completion)
  if (( CURRENT == 2 )); then
    _describe 'command' commands
    return
  fi
  case "${words[2]}" in
    vpn) _values 'subcommand' up down status servers switch config;;
    proxy) _values 'subcommand' up down status url;;
    challenge) _values 'subcommand' list info start stop download;;
    config) _values 'subcommand' list get set;;
    start|info|stop|reset|extend|todo|shell)
      local cache="${XDG_CACHE_HOME:-$HOME/.cache}/htb-cli/machines.json"
      [[ -f $cache ]] && _values 'machine' ${(f)"$(grep -o '"name": "[^"]*"' $cache | cut -d'\"' -f4)"};;
  esac
}
_htb "$@"
"""


def completion(args):
    print(ZSH_COMPLETION if args.shell_name == "zsh" else BASH_COMPLETION)
