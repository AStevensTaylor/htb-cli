"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from . import __version__, api, ui, vpn as vpn_mod
from .commands import auth, extras, flag, machine, net

SUPPRESS = argparse.SUPPRESS

GLOBAL_DEFAULTS = {
    "json": False, "debug": False, "yes": False, "quiet": False,
    "netns": None, "global_vpn": False, "tcp": False,
    "no_internet": False, "no_vpn": False, "no_hosts": False, "shell": False,
    "machine": None, "product": None, "no_proxy": False, "no_veth": False,
    "proxy_port": None, "proxy_auth": None, "proxy_localhost": False,
    "listen": None, "port": None, "scheme": "socks5",
}

EPILOG = """\
examples:
  htb login                        store your App Token
  htb search --os linux --state active --difficulty easy
  htb start Lame --shell           spawn + VPN + drop into a connected shell
  htb shell                        shell on the HTB network (nothing else is)
  htb exec -- nmap -sV $HTB_TARGET
  htb proxy url                    socks5://… for Burp, ZAP, curl on the host
  htb submit                       prompt for a flag for the running machine
  htb vpn status
"""


def base_flags(parser):
    parser.add_argument("--json", action="store_true", default=SUPPRESS,
                        help="machine-readable output")
    parser.add_argument("-y", "--yes", action="store_true", default=SUPPRESS,
                        help="assume yes / pick the first match")
    parser.add_argument("-q", "--quiet", action="store_true", default=SUPPRESS,
                        help="suppress progress chatter")
    parser.add_argument("--debug", action="store_true", default=SUPPRESS,
                        help="log every API request")
    return parser


def vpn_flags(parser):
    parser.add_argument("--netns", metavar="NAME", default=SUPPRESS,
                        help="network namespace to use (default: htb)")
    parser.add_argument("--global", dest="global_vpn", action="store_true", default=SUPPRESS,
                        help="connect the whole machine instead of a namespace")
    parser.add_argument("--tcp", action="store_true", default=SUPPRESS,
                        help="use the TCP OpenVPN config")
    parser.add_argument("--no-internet", action="store_true", default=SUPPRESS,
                        help="namespace reaches the lab only, no NAT uplink")
    parser.add_argument("--no-proxy", action="store_true", default=SUPPRESS,
                        help="do not run the in-namespace SOCKS/HTTP proxy")
    parser.add_argument("--no-veth", action="store_true", default=SUPPRESS,
                        help="no host link at all (rules out the proxy and internet)")
    return parser


def proxy_flags(parser):
    parser.add_argument("--proxy-port", type=int, default=SUPPRESS, metavar="PORT",
                        help="proxy port (default 1080)")
    parser.add_argument("--proxy-auth", metavar="USER:PASS", default=SUPPRESS,
                        help="require these proxy credentials")
    parser.add_argument("--proxy-localhost", action="store_true", default=SUPPRESS,
                        help="also expose the proxy on 127.0.0.1")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = base_flags(argparse.ArgumentParser(add_help=False))
    netgrp = vpn_flags(base_flags(argparse.ArgumentParser(add_help=False)))

    parser = argparse.ArgumentParser(
        prog="htb", description="Hack The Box from the command line.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common])
    parser.add_argument("-V", "--version", action="version", version=f"htb-cli {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # --- auth ---------------------------------------------------------------
    p = sub.add_parser("login", parents=[common], help="store your HTB App Token")
    p.add_argument("token", nargs="?", help="token (omit to be prompted)")
    p.set_defaults(func=auth.login)

    sub.add_parser("logout", parents=[common], help="forget the stored token"
                   ).set_defaults(func=auth.logout)
    sub.add_parser("whoami", parents=[common], help="show the logged-in user"
                   ).set_defaults(func=auth.whoami)
    sub.add_parser("status", parents=[netgrp], help="user, VPN and active machine"
                   ).set_defaults(func=auth.status)

    # --- discovery ----------------------------------------------------------
    p = sub.add_parser("search", parents=[common], aliases=["machines", "ls"],
                       help="search the machine catalogue")
    p.add_argument("query", nargs="*", help="name fragment")
    p.add_argument("--os", help="Linux, Windows, FreeBSD…")
    p.add_argument("-d", "--difficulty", help="Easy, Medium, Hard, Insane…")
    p.add_argument("--state", default="active",
                   choices=["active", "retired", "unreleased", "all"],
                   help="default: active")
    p.add_argument("--todo", action="store_true", help="only machines on your to-do list")
    p.add_argument("--free", action="store_true", help="only free-tier machines")
    p.add_argument("--owned", dest="owned", action="store_const", const=True, default=None,
                   help="only machines you have rooted")
    p.add_argument("--unowned", dest="owned", action="store_const", const=False,
                   help="only machines you have not rooted")
    p.add_argument("--sort", choices=["relevance", "name", "difficulty", "release"],
                   default="relevance")
    p.add_argument("-n", "--limit", type=int, default=25, help="0 for everything")
    p.add_argument("--refresh", action="store_true", help="bypass the local cache")
    p.set_defaults(func=machine.search)

    p = sub.add_parser("info", parents=[common], help="details for one machine")
    p.add_argument("machine")
    p.set_defaults(func=machine.info)

    # --- lifecycle ----------------------------------------------------------
    p = proxy_flags(sub.add_parser("start", parents=[netgrp], aliases=["spawn"],
                                   help="spawn a machine and connect to its network"))
    p.add_argument("machine")
    p.add_argument("-s", "--shell", action="store_true",
                   help="drop into a shell attached to the lab when it is up")
    p.add_argument("--no-vpn", action="store_true", help="do not touch the VPN")
    p.add_argument("--no-hosts", action="store_true", help="do not write a hosts entry")
    p.set_defaults(func=machine.start)

    p = sub.add_parser("stop", parents=[netgrp], aliases=["terminate"],
                       help="stop the running machine")
    p.add_argument("machine", nargs="?")
    p.add_argument("--vpn-down", action="store_true", help="also disconnect the VPN")
    p.set_defaults(func=machine.stop)

    p = sub.add_parser("reset", parents=[common], help="reset the running machine")
    p.add_argument("machine", nargs="?")
    p.set_defaults(func=machine.reset)

    p = sub.add_parser("extend", parents=[common], help="extend the running machine")
    p.add_argument("machine", nargs="?")
    p.set_defaults(func=machine.extend)

    sub.add_parser("active", parents=[common], help="what is running right now"
                   ).set_defaults(func=machine.active)
    sub.add_parser("ip", parents=[common], aliases=["target"],
                   help="print the target IP and nothing else"
                   ).set_defaults(func=machine.ip)

    p = sub.add_parser("todo", parents=[common], help="toggle a machine on your to-do list")
    p.add_argument("machine")
    p.set_defaults(func=machine.todo)

    # --- flags --------------------------------------------------------------
    p = sub.add_parser("submit", parents=[common], aliases=["own", "flag"],
                       help="submit a flag")
    p.add_argument("flag", nargs="?", help="omit to be prompted without echo")
    p.add_argument("-m", "--machine", help="machine (default: the running one)")
    p.add_argument("-c", "--challenge", help="submit a challenge flag instead")
    p.add_argument("--fortress", help="fortress id")
    p.add_argument("--prolab", help="prolab id")
    p.add_argument("--arena", action="store_true", help="seasonal release arena machine")
    p.add_argument("-d", "--difficulty", type=int, choices=range(1, 11), metavar="1-10",
                   help="your difficulty rating")
    p.set_defaults(func=flag.submit)

    # --- namespace shell ----------------------------------------------------
    p = proxy_flags(sub.add_parser(
        "shell", parents=[netgrp],
        help="shell whose network is the HTB lab (and only it)"))
    p.add_argument("machine", nargs="?", help="spawn this machine first")
    p.add_argument("--no-vpn", action="store_true", help="assume the VPN is already up")
    p.add_argument("--no-hosts", action="store_true", help="do not write a hosts entry")
    p.set_defaults(func=net.shell)

    p = sub.add_parser("exec", parents=[netgrp],
                       help="run one command inside the lab namespace")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command to run, e.g. -- nmap -sV $HTB_TARGET")
    p.set_defaults(func=net.exec_cmd)

    # --- vpn ----------------------------------------------------------------
    vpn_parser = sub.add_parser("vpn", parents=[common], help="VPN connection management")
    vpn_sub = vpn_parser.add_subparsers(dest="vpn_command", metavar="<subcommand>")
    vpn_parser.set_defaults(func=lambda a: vpn_parser.print_help())

    p = proxy_flags(vpn_sub.add_parser("up", parents=[netgrp], help="connect"))
    p.add_argument("--product", choices=list(vpn_mod.PRODUCTS),
                   help="labs (default), starting_point, competitive, fortresses")
    p.add_argument("--force", action="store_true", help="reconnect even if already up")
    p.set_defaults(func=net.vpn_up)

    p = vpn_sub.add_parser("down", parents=[netgrp], help="disconnect and clean up")
    p.add_argument("--force", action="store_true", help="tear down even if nothing looks up")
    p.set_defaults(func=net.vpn_down)

    vpn_sub.add_parser("status", parents=[netgrp], help="local and HTB-side status"
                       ).set_defaults(func=net.vpn_status)

    p = vpn_sub.add_parser("servers", parents=[common], help="list VPN servers")
    p.add_argument("--product")
    p.set_defaults(func=net.vpn_servers)

    p = vpn_sub.add_parser("switch", parents=[common], help="change assigned VPN server")
    p.add_argument("server", help="server id or name fragment")
    p.add_argument("--product")
    p.set_defaults(func=net.vpn_switch)

    p = vpn_sub.add_parser("config", parents=[common], help="download an .ovpn file")
    p.add_argument("-o", "--output")
    p.add_argument("--product")
    p.add_argument("--tcp", action="store_true", default=SUPPRESS)
    p.set_defaults(func=net.vpn_config)

    # --- proxy --------------------------------------------------------------
    proxy_parser = sub.add_parser(
        "proxy", parents=[common],
        help="SOCKS5/HTTP proxy into the lab, for Burp and friends")
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_command", metavar="<subcommand>")
    proxy_parser.set_defaults(func=net.proxy_status)

    p = proxy_sub.add_parser("up", parents=[common],
                             help="start the proxy in the namespace")
    p.add_argument("--netns", metavar="NAME", default=SUPPRESS)
    p.add_argument("--port", type=int, help="port to listen on (default 1080)")
    p.add_argument("--listen", metavar="ADDR",
                   help="bind address inside the namespace (default 10.200.200.2)")
    p.add_argument("--auth", dest="proxy_auth", metavar="USER:PASS",
                   help="require these credentials")
    p.add_argument("--localhost", dest="proxy_localhost", action="store_true",
                   help="also expose it on 127.0.0.1")
    p.set_defaults(func=net.proxy_up)

    proxy_sub.add_parser("down", parents=[netgrp], help="stop the proxy"
                         ).set_defaults(func=net.proxy_down)
    proxy_sub.add_parser("status", parents=[netgrp], help="proxy state and endpoint"
                         ).set_defaults(func=net.proxy_status)

    p = proxy_sub.add_parser("url", parents=[netgrp],
                             help="print the proxy URL for scripts")
    p.add_argument("--scheme", default="socks5", choices=["socks5", "socks5h", "http"])
    p.set_defaults(func=net.proxy_url)

    # --- challenges ---------------------------------------------------------
    ch = sub.add_parser("challenge", parents=[common], aliases=["ch"], help="challenges")
    ch_sub = ch.add_subparsers(dest="challenge_command", metavar="<subcommand>")
    ch.set_defaults(func=lambda a: ch.print_help())

    p = ch_sub.add_parser("list", parents=[common], help="list challenges")
    p.add_argument("query", nargs="*")
    p.add_argument("--state", default="active", choices=["active", "retired", "unreleased"])
    p.add_argument("--category")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.set_defaults(func=extras.challenge_list)

    for name, func, helptext in (
        ("info", extras.challenge_info, "challenge details"),
        ("start", extras.challenge_start, "start the challenge container"),
        ("stop", extras.challenge_stop, "stop the challenge container"),
    ):
        p = ch_sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("challenge")
        p.set_defaults(func=func)

    p = ch_sub.add_parser("download", parents=[common], help="download challenge files")
    p.add_argument("challenge")
    p.add_argument("-o", "--output")
    p.set_defaults(func=extras.challenge_download)

    # --- misc ---------------------------------------------------------------
    sub.add_parser("season", parents=[common], help="seasonal progress and release box"
                   ).set_defaults(func=extras.season)
    sub.add_parser("refresh", parents=[common], help="refresh the machine cache"
                   ).set_defaults(func=extras.refresh)

    p = sub.add_parser("config", parents=[common], help="read or write cli settings")
    p.add_argument("action", choices=["list", "get", "set"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=extras.config_cmd)

    p = sub.add_parser("raw", parents=[common], help="call any API endpoint")
    p.add_argument("method", choices=["GET", "POST", "get", "post"])
    p.add_argument("path", help="e.g. /machine/active")
    p.add_argument("--data", help="JSON body for POST")
    p.set_defaults(func=extras.raw)

    p = sub.add_parser("completion", parents=[common], help="print a shell completion script")
    p.add_argument("shell_name", nargs="?", default="bash", choices=["bash", "zsh"])
    p.set_defaults(func=extras.completion)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for key, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)

    ui.JSON_MODE = args.json
    ui.QUIET = args.quiet or args.json

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    try:
        args.func(args)
    except KeyboardInterrupt:
        print()
        return 130
    except api.AuthError as exc:
        ui.error(exc.message)
        return 3
    except api.ApiError as exc:
        ui.error(exc.message)
        if args.debug and exc.body:
            ui.warn(exc.body[:2000])
        return 4
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
