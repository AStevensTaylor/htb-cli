# htb-cli

Hack The Box from the terminal: spawn a machine, get on its network, hack, submit the flag.

The headline feature is `htb shell` — the lab VPN runs inside a dedicated **network
namespace**, so the box you spawn is reachable from that shell *and nothing else on your
system*. No system-wide routes, no "why is my DNS weird", no VPN left running after you
walk away from a box.

A SOCKS5/HTTP proxy runs inside that namespace too, so GUI tools that live on your normal
desktop — Burp, ZAP, a browser — reach the lab by pointing at an upstream proxy instead of
having to be launched inside the namespace.

```
$ htb start Blurry --shell
:: sudo is needed to start OpenVPN for namespace htb
✓ Connected to EU Free 1 (labs)
:: Machine deployed to lab.
✓ Blurry is up
  target      10.10.11.24
  hostname    blurry.htb

── htb shell ──────────────────────────────
  namespace  htb
  target     10.10.11.24
  note       only this shell is on the HTB network - exit to leave

⬢ htb Blurry [personal@arch ~]$ nmap -sV $HTB_TARGET
```

```
$ htb proxy status
endpoint   10.200.200.2:1080
protocols  socks5, http connect, http absolute-url
reachable  yes
```

## Install

Needs Python 3.9+, `openvpn`, `iproute2` and `sudo`. No Python dependencies.

```bash
# run it straight from the checkout
./htb-cli --help

# or install the `htb` command
uv tool install .          # or: pipx install .
```

Shell completion:

```bash
sudo cp completions/htb.bash /usr/share/bash-completion/completions/htb   # bash
cp completions/_htb ~/.zfunc/_htb                                         # zsh (in $fpath)
```

## Authenticate

Create an **App Token** at <https://app.hackthebox.com/profile/settings>, then:

```bash
htb login                  # prompts, echo off; stored 0600 in ~/.config/htb-cli/token
htb login "$TOKEN"         # or non-interactively
HTB_TOKEN=... htb whoami    # or skip storage entirely
```

## The three things you asked for

**Start a machine and be on its network immediately**

```bash
htb start Blurry              # VPN up (namespace) + spawn + wait for IP + hosts entry
htb start Blurry --shell      # …and drop into a shell that can reach it
htb shell                     # shell for whatever is already running
htb exec -- nmap -sV '$HTB_TARGET'
```

`htb start` picks the right VPN product automatically (labs / starting point / release
arena), reuses an existing connection, and writes `blurry.htb` into the namespace's hosts
file so you can `curl http://blurry.htb` right away.

**Submit flags**

```bash
htb submit                             # prompts without echo, for the running machine
htb submit 8f2a…c1 -m Blurry -d 6      # explicit machine + difficulty rating
htb submit -c "Baby RE" -d 3           # challenge flag
htb submit --arena                     # seasonal release-arena machine
```

**Reach the lab from Burp (or anything else on the host)**

`htb vpn up` and `htb start` also launch a SOCKS5 + HTTP proxy *inside* the namespace,
bound to the namespace end of the veth pair — an address only your host can reach:

```bash
htb proxy status                  # endpoint, reachability, pid, log
htb proxy url                     # socks5://10.200.200.2:1080
curl --socks5-hostname 10.200.200.2:1080 http://blurry.htb/
proxychains -q nmap -sT 10.10.11.24          # socks5 10.200.200.2 1080
```

In **Burp**: Settings → Network → Connections → Upstream proxy servers → add a rule for
destination `*` with proxy type **SOCKS5**, host `10.200.200.2`, port `1080`, and tick *Do
DNS lookups over SOCKS proxy* so `blurry.htb` resolves inside the namespace. (ZAP: Network
→ Connection → SOCKS proxy. Firefox: SOCKS v5 + *Proxy DNS when using SOCKS v5*.)

Useful options:

```bash
htb proxy up --port 9150 --auth burp:s3cret   # different port, require credentials
htb proxy up --localhost                      # also listen on 127.0.0.1:1080
htb vpn up --no-proxy                         # skip it entirely
htb config set proxy false                    # …permanently
```

Because the proxy process lives in the namespace, **name resolution happens there**: the
hosts entry `htb start` writes and any DNS the lab pushes both apply, while your host still
knows nothing about `blurry.htb`. It speaks SOCKS5 CONNECT, HTTP CONNECT and absolute-URL
HTTP on the same port; SOCKS BIND and UDP ASSOCIATE are not implemented (nothing in a
normal web workflow uses them).

**HTB Academy**

Academy targets sit behind their own VPN. Academy has no App Token API, so download the
`.ovpn` from <https://academy.hackthebox.com/vpn> once and hand it over. It is stored
0600 as `~/.config/htb-cli/academy.ovpn` and reused after that:

```bash
htb vpn up --ovpn ~/Downloads/academy-regular.ovpn   # import + connect (implies --academy)
htb vpn up --academy                                 # later: reuse the imported config
htb shell --academy --target 10.129.42.17            # namespace shell, $HTB_TARGET set
```

It uses the same namespace, proxy and cleanup as the labs VPN. Academy and the labs use
overlapping address ranges, so one namespace holds one of them at a time: `htb start`
switches back to the labs automatically. To keep both up at once, give Academy its own
namespace with `--netns academy`. No labs token is needed for any of this.

**Search**

```bash
htb search blur                        # fuzzy name match
htb search --os Linux --difficulty Easy --state active
htb search --todo                      # your to-do list
htb search --unowned --state retired --sort release -n 50
htb info Blurry                        # full profile, tags, your progress
```

The catalogue is cached under `~/.cache/htb-cli` (6 h); `--refresh` or `htb refresh`
re-pulls it. Search is local and instant, so it also feeds shell completion.

## Everything else

| Command | What it does |
| --- | --- |
| `htb status` | account, VPN (local + what HTB thinks), running machine |
| `htb active` / `htb ip` | running machine / just its IP, for `nmap $(htb ip)` |
| `htb stop [machine] [--vpn-down]` | terminate, optionally disconnect |
| `htb reset` / `htb extend` | reset or extend the running machine |
| `htb todo <machine>` | toggle the machine on your to-do list |
| `htb vpn up\|down\|status\|servers\|switch\|config` | VPN control, server list/switch, `.ovpn` download (`up --academy` for Academy) |
| `htb proxy up\|down\|status\|url` | the in-namespace SOCKS5/HTTP proxy for host-side tools |
| `htb challenge list\|info\|start\|stop\|download` | challenges (download password: `hackthebox`) |
| `htb season` | season tier, rank and this week's release box |
| `htb raw GET /machine/active` | call any endpoint; escape hatch for an undocumented API |
| `htb config list\|get\|set` | defaults: namespace name, product, hosts handling… |
| `--json` | any command, machine-readable |

## How the isolation works

```
        your desktop                            network namespace "htb"
  ┌────────────────────────┐             ┌────────────────────────────────────┐
  │ browser, mail, …       │             │  htb shell / htb exec              │
  │ normal default route   │             │                                    │
  │                        │             │  htb.socks ──┐                     │
  │ Burp ──socks5──────────┼────────────▶│  10.200.200.2:1080                 │
  └────────────┬───────────┘   veth      │              ├── htb-htb (tun) ────┼──▶ 10.10.x
               │                         │              └── veth ──┐          │    10.129.x
               │                         └─────────────────────────┼──────────┘   (HTB lab)
               │      NAT (iptables)                               │
               └───────────────────────────────────────────────────┘ ──▶ internet
```

* OpenVPN runs in the root namespace (so it can reach HTB normally), but starts with
  `--ifconfig-noexec --route-noexec`; its `--up` script moves the tun device into the
  namespace and its `--route-up` script installs the pushed lab routes *there*.
* Your normal shells never see those routes — lab traffic is opt-in, per shell.
* A veth pair links the namespace to the host. It carries two things: the proxy socket that
  host-side tools connect to, and — when a masquerade rule and a default route are added —
  internet access, so you can still `pip install` or pull an exploit while you work.
  `--no-internet` (or `htb config set netns_internet false`) keeps the link but drops the
  uplink, leaving the namespace lab-only with the proxy still reachable; `--no-veth` removes
  the link entirely (and with it the proxy).
* The proxy binds to the namespace end of that /30, so only this host can talk to it — the
  same exposure as binding to localhost. `--auth user:pass` adds credentials if you share
  the machine.
* DNS pushed by the lab (prolabs, fortresses) is installed in the namespace only, and only
  when it is actually routable.
* Hosts entries go to `/etc/netns/htb/hosts`, which `ip netns exec` bind-mounts over
  `/etc/hosts` *inside the namespace* — your real `/etc/hosts` is never modified. The file
  is seeded when the namespace is created and later rewritten in place, so processes already
  running in there (the proxy included) pick up new entries.
* The namespace also gets its own `/etc/nsswitch.conf` with `hosts: files dns`. Without it,
  systemd-resolved's NSS module — still reachable over the shared `/run` socket — answers
  first and returns NOTFOUND, and the namespace hosts file would never be read.
* `htb vpn down` kills OpenVPN and removes the namespace, veth, NAT rules and
  `net.ipv4.ip_forward` change (restoring the previous value).

Prefer the old behaviour? `htb vpn up --global` / `htb start X --global` connects the whole
machine instead.

### Root access

Three things need root: starting OpenVPN, creating the namespace, and entering it. Every
privileged step is a generated shell script under `~/.local/state/htb-cli/vpn/` (mode 0700)
run with a single `sudo` per action — read them before you run them if you like. Commands
inside the namespace drop back to your own uid with `setpriv`, so `htb shell` is *not* a
root shell.

## Development

```bash
python3 tests/smoke.py      # every command against a fake API, no account needed
python3 tests/proxy.py      # SOCKS5/HTTP proxy protocol tests, loopback only
python3 tests/vpnswitch.py  # picking the lab server a machine lives on
python3 tests/academy.py    # Academy .ovpn import, kept off the labs API
```

## Notes and limits

* The HTB v4 API is undocumented; endpoints live in one place (`htb/api.py`) so they are
  easy to patch if HTB moves something. `htb raw` reaches anything not wrapped yet.
* `/vm/spawn` starts machines on every tier: free accounts land on a shared lab server,
  VIP/VIP+ get a private instance. (HTB retired the old free-only `/machine/play` route.)
* A machine can live behind a lab server that is not the one you are connected to (free
  machines especially). `htb start`/`htb shell` notice this and reconnect to the right
  server; `htb vpn switch <id>` does it by hand.
* Namespaces are a Linux feature — the `shell`/`exec` commands need Linux. Everything else
  (search, submit, info, VPN config download) works anywhere Python does.
* `htb vpn down` stops the proxy and does not kill shells already inside the namespace; they
  simply lose the tunnel. Exit them normally.
* If a client resolves a name itself and hands the proxy an IPv6 literal, the connection
  fails — the namespace is IPv4-only, like the labs. Use remote DNS (`--socks5-hostname`,
  `socks5h://`, or Burp's *Do DNS lookups over SOCKS proxy*).
* Not affiliated with or endorsed by Hack The Box.
