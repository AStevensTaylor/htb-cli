"""Configuration, token storage and on-disk paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP = "htb-cli"

DEFAULTS = {
    # Name of the network namespace used for the isolated VPN.
    "netns": "htb",
    # "labs" | "starting_point" | "competitive" | "fortresses"
    "vpn_product": "labs",
    # "udp" | "tcp"
    "vpn_protocol": "udp",
    # Give the namespace internet access through a NAT'd veth pair.
    "netns_internet": True,
    # Link the namespace to the host with a veth pair (needed by the proxy).
    "netns_veth": True,
    # Run a SOCKS5/HTTP proxy inside the namespace for host-side tools.
    "proxy": True,
    "proxy_port": 1080,
    # "user:pass" to require credentials, or null for an open local proxy.
    "proxy_auth": None,
    # Also expose the proxy on 127.0.0.1 via a forwarder.
    "proxy_localhost": False,
    # Manage /etc/hosts entries for spawned machines.
    "manage_hosts": True,
    # Shell to launch with `htb shell` (defaults to $SHELL).
    "shell": None,
    # Seconds a cached machine catalogue stays fresh.
    "cache_ttl": 21600,
}


def _dir(env_var: str, xdg_var: str, fallback: str) -> Path:
    if os.environ.get(env_var):
        return Path(os.environ[env_var]).expanduser()
    base = os.environ.get(xdg_var) or str(Path.home() / fallback)
    return Path(base).expanduser() / APP


CONFIG_DIR = _dir("HTB_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")
CACHE_DIR = _dir("HTB_CACHE_DIR", "XDG_CACHE_HOME", ".cache")
STATE_DIR = _dir("HTB_STATE_DIR", "XDG_STATE_HOME", ".local/state")

CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "token"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    ensure_dirs()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    CONFIG_FILE.chmod(0o600)


def get(key: str, default=None):
    return load().get(key, DEFAULTS.get(key, default))


def set_(key: str, value) -> None:
    cfg = load()
    cfg[key] = value
    save(cfg)


def get_token() -> str | None:
    """Token from $HTB_TOKEN, else the token file."""
    env = os.environ.get("HTB_TOKEN")
    if env:
        return env.strip()
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return token or None


def set_token(token: str) -> None:
    ensure_dirs()
    TOKEN_FILE.write_text(token.strip() + "\n")
    TOKEN_FILE.chmod(0o600)


def clear_token() -> bool:
    try:
        TOKEN_FILE.unlink()
        return True
    except OSError:
        return False
