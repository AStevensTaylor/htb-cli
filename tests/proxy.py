"""Protocol tests for the in-namespace proxy (htb/socks.py).

    python3 tests/proxy.py

Runs entirely on loopback: no root, no namespace, no HTB account.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from htb.socks import Forwarder, Proxy  # noqa: E402

logging.getLogger("htb.socks").setLevel(logging.CRITICAL)

BODY = b"hi"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n" + BODY
checks = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append(ok)
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  ({detail})" if detail and not ok else ""))


async def origin(reader, writer):
    """A minimal HTTP origin server standing in for a lab web server."""
    try:
        await reader.read(4096)
        writer.write(RESPONSE)
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def listen(handler) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def socks_open(port, host, target_port, atyp=1, auth=None, command=1):
    """Perform a SOCKS5 negotiation; returns (streams, reply_code)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    method = 2 if auth else 0
    writer.write(bytes([5, 1, method]))
    await writer.drain()
    chosen = await reader.readexactly(2)
    if chosen[1] == 0xFF:
        return (reader, writer), 0xFF
    if auth:
        user, password = auth
        writer.write(bytes([1, len(user)]) + user.encode()
                     + bytes([len(password)]) + password.encode())
        await writer.drain()
        if (await reader.readexactly(2))[1] != 0:
            return (reader, writer), 0xF1  # auth rejected

    if atyp == 1:
        address = socket.inet_aton(host)
    else:
        address = bytes([len(host)]) + host.encode()
    writer.write(bytes([5, command, 0, atyp]) + address + struct.pack("!H", target_port))
    await writer.drain()
    reply = await reader.readexactly(4)
    await reader.readexactly(4 if reply[3] == 1 else 16)
    await reader.readexactly(2)
    return (reader, writer), reply[1]


async def http_get(reader, writer, host) -> bytes:
    writer.write(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    return data


async def main() -> int:
    origin_server, origin_port = await listen(origin)
    open_server, open_port = await listen(Proxy())
    auth_server, auth_port = await listen(Proxy(auth=("burp", "s3cret")))
    fwd_server, fwd_port = await listen(Forwarder("127.0.0.1", open_port))
    closed_port = free_port()

    print("socks5")
    streams, code = await socks_open(open_port, "127.0.0.1", origin_port)
    check("connect to an IPv4 literal", code == 0, f"reply {code}")
    check("data flows through the tunnel",
          BODY in await http_get(*streams, "127.0.0.1"))

    streams, code = await socks_open(open_port, "localhost", origin_port, atyp=3)
    check("connect to a hostname (resolved proxy-side)", code == 0, f"reply {code}")
    check("hostname tunnel carries data", BODY in await http_get(*streams, "localhost"))

    _, code = await socks_open(open_port, "127.0.0.1", closed_port)
    check("refused target reports 'connection refused'", code == 5, f"reply {code}")

    _, code = await socks_open(open_port, "127.0.0.1", origin_port, command=2)
    check("BIND is refused with 'command not supported'", code == 7, f"reply {code}")

    print("socks5 with credentials")
    _, code = await socks_open(auth_port, "127.0.0.1", origin_port)
    check("no-auth client is turned away", code == 0xFF, f"reply {code}")
    _, code = await socks_open(auth_port, "127.0.0.1", origin_port, auth=("burp", "wrong"))
    check("wrong password is rejected", code == 0xF1, f"reply {code}")
    streams, code = await socks_open(auth_port, "127.0.0.1", origin_port,
                                     auth=("burp", "s3cret"))
    check("correct password is accepted", code == 0, f"reply {code}")
    streams[1].close()

    print("http")
    reader, writer = await asyncio.open_connection("127.0.0.1", open_port)
    writer.write(f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    status = await reader.readuntil(b"\r\n\r\n")
    check("CONNECT establishes a tunnel", b"200" in status, status[:40].decode())
    check("CONNECT tunnel carries data", BODY in await http_get(reader, writer, "x"))

    reader, writer = await asyncio.open_connection("127.0.0.1", open_port)
    writer.write(f"GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\n"
                 f"Host: 127.0.0.1:{origin_port}\r\nProxy-Connection: keep-alive\r\n\r\n"
                 .encode())
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    check("absolute-form GET is forwarded", BODY in data, data[:60].decode("latin-1"))

    reader, writer = await asyncio.open_connection("127.0.0.1", auth_port)
    writer.write(b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    data = await reader.read(200)
    writer.close()
    check("unauthenticated http gets 407", b"407" in data, data[:40].decode("latin-1"))

    token = base64.b64encode(b"burp:s3cret").decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", auth_port)
    writer.write(f"GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\nHost: x\r\n"
                 f"Proxy-Authorization: Basic {token}\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read(4096)
    writer.close()
    check("authenticated http is forwarded", BODY in data, data[:60].decode("latin-1"))

    reader, writer = await asyncio.open_connection("127.0.0.1", open_port)
    writer.write(b"\x01\x02\x03")
    await writer.drain()
    check("garbage is dropped without killing the server", await reader.read(8) == b"")

    print("localhost forwarder")
    streams, code = await socks_open(fwd_port, "127.0.0.1", origin_port)
    check("socks5 works through the forwarder", code == 0, f"reply {code}")
    check("forwarded tunnel carries data", BODY in await http_get(*streams, "127.0.0.1"))

    for server in (origin_server, open_server, auth_server, fwd_server):
        server.close()

    failed = checks.count(False)
    print()
    if failed:
        print(f"\033[1;41m {failed} of {len(checks)} checks failed \033[0m")
        return 1
    print(f"\033[1;42m all {len(checks)} proxy checks passed \033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
