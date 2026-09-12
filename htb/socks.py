"""A small SOCKS5 / HTTP proxy, standard library only.

htb-cli runs this *inside* the lab network namespace and binds it to the
namespace end of the veth pair, so tools that live on the host (Burp, ZAP,
curl, proxychains) can reach the lab by pointing at an upstream proxy instead
of being launched inside the namespace themselves.

Because the process runs in the namespace, name resolution happens there too:
`blurry.htb` resolves through the namespace hosts file and any DNS the lab
pushed.

Run standalone with:

    python3 -m htb.socks --listen 10.200.200.2 --port 1080
    python3 -m htb.socks --forward 10.200.200.2:1080 --listen 127.0.0.1
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import errno
import ipaddress
import logging
import os
import signal
import socket
import struct
import sys
from urllib.parse import urlsplit

LOG = logging.getLogger("htb.socks")

VERSION = 5
NO_AUTH = 0x00
USER_PASS = 0x02
NO_ACCEPTABLE = 0xFF

CMD_CONNECT = 0x01
ATYP_IPV4, ATYP_DOMAIN, ATYP_IPV6 = 0x01, 0x03, 0x04

(REP_OK, REP_FAIL, REP_NOT_ALLOWED, REP_NET_UNREACH, REP_HOST_UNREACH,
 REP_REFUSED, REP_TTL, REP_CMD, REP_ATYP) = range(9)

HEAD_LIMIT = 64 * 1024
CHUNK = 65536
HOP_BY_HOP = {"proxy-connection", "connection", "keep-alive", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade"}


def _peer(writer: asyncio.StreamWriter) -> str:
    info = writer.get_extra_info("peername") or ("?", 0)
    return f"{info[0]}:{info[1]}" if isinstance(info, tuple) else str(info)


def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except OSError:
        pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:
            pass


async def _splice(client_r, client_w, remote_r, remote_w) -> None:
    await asyncio.gather(_pipe(client_r, remote_w), _pipe(remote_r, client_w))
    _close(remote_w)
    _close(client_w)


class Proxy:
    """SOCKS5 CONNECT plus HTTP CONNECT / absolute-form proxying."""

    def __init__(self, auth: tuple | None = None, timeout: float = 15.0):
        self.auth = auth
        self.timeout = timeout

    async def __call__(self, reader, writer) -> None:
        who = _peer(writer)
        try:
            first = await asyncio.wait_for(reader.readexactly(1), 60)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            _close(writer)
            return
        try:
            if first == b"\x05":
                await self._socks5(reader, writer, who)
            elif first.isalpha():
                await self._http(reader, writer, first, who)
            else:
                LOG.warning("%s rejected: not SOCKS5 or HTTP (first byte %r)", who, first)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except OSError as exc:
            LOG.debug("%s connection error: %s", who, exc)
        except Exception as exc:  # never let one client kill the server
            LOG.warning("%s failed: %s", who, exc)
        finally:
            _close(writer)

    # -- shared ------------------------------------------------------------

    async def _open(self, host: str, port: int):
        """Connect out from inside the namespace; returns streams or a reason."""
        try:
            streams = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.timeout)
            return streams, None
        except asyncio.TimeoutError:
            return None, (REP_TTL, "504 Gateway Timeout", "timed out")
        except socket.gaierror:
            return None, (REP_HOST_UNREACH, "502 Bad Gateway", "cannot resolve")
        except ConnectionRefusedError:
            return None, (REP_REFUSED, "502 Bad Gateway", "connection refused")
        except OSError as exc:
            code = (REP_NET_UNREACH if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)
                    else REP_FAIL)
            return None, (code, "502 Bad Gateway", os.strerror(exc.errno or 0) or str(exc))

    # -- socks5 ------------------------------------------------------------

    async def _socks5(self, reader, writer, who) -> None:
        count = (await reader.readexactly(1))[0]
        offered = set(await reader.readexactly(count))
        wanted = USER_PASS if self.auth else NO_AUTH
        if wanted not in offered:
            writer.write(bytes([VERSION, NO_ACCEPTABLE]))
            await writer.drain()
            LOG.warning("%s rejected: no acceptable authentication method", who)
            return
        writer.write(bytes([VERSION, wanted]))
        await writer.drain()

        if self.auth and not await self._socks5_auth(reader, writer, who):
            return

        header = await reader.readexactly(4)
        if header[0] != VERSION:
            return
        command, atyp = header[1], header[3]
        host = await self._read_address(reader, atyp)
        if host is None:
            await self._socks5_reply(writer, REP_ATYP)
            return
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        if command != CMD_CONNECT:
            LOG.warning("%s rejected: SOCKS command %d is not supported", who, command)
            await self._socks5_reply(writer, REP_CMD)
            return

        streams, failure = await self._open(host, port)
        if failure:
            code, _, reason = failure
            LOG.info("socks5 %s -> %s:%d failed (%s)", who, host, port, reason)
            await self._socks5_reply(writer, code)
            return

        remote_r, remote_w = streams
        bound = remote_w.get_extra_info("sockname") or ("0.0.0.0", 0)
        await self._socks5_reply(writer, REP_OK, bound[0], bound[1])
        LOG.info("socks5 %s -> %s:%d", who, host, port)
        await _splice(reader, writer, remote_r, remote_w)

    async def _socks5_auth(self, reader, writer, who) -> bool:
        if (await reader.readexactly(1))[0] != 0x01:
            return False
        user = (await reader.readexactly((await reader.readexactly(1))[0])).decode(
            "utf-8", "replace")
        password = (await reader.readexactly((await reader.readexactly(1))[0])).decode(
            "utf-8", "replace")
        ok = (user, password) == self.auth
        writer.write(bytes([0x01, 0x00 if ok else 0x01]))
        await writer.drain()
        if not ok:
            LOG.warning("%s rejected: bad proxy credentials", who)
        return ok

    @staticmethod
    async def _read_address(reader, atyp) -> str | None:
        if atyp == ATYP_IPV4:
            return socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        if atyp == ATYP_IPV6:
            return socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        if atyp == ATYP_DOMAIN:
            length = (await reader.readexactly(1))[0]
            raw = await reader.readexactly(length)
            try:
                # ascii hostnames pass straight through; punycode is decoded
                return raw.decode("idna")
            except (UnicodeError, ValueError):
                # the idna codec has no lenient error handler, so fall back
                return raw.decode("utf-8", "replace")
        return None

    @staticmethod
    async def _socks5_reply(writer, code, address="0.0.0.0", port=0) -> None:
        try:
            parsed = ipaddress.ip_address(address)
            atyp = ATYP_IPV4 if parsed.version == 4 else ATYP_IPV6
            packed = parsed.packed
        except ValueError:
            atyp, packed = ATYP_IPV4, b"\x00\x00\x00\x00"
        writer.write(bytes([VERSION, code, 0x00, atyp]) + packed + struct.pack("!H", port))
        await writer.drain()

    # -- http --------------------------------------------------------------

    async def _http(self, reader, writer, first, who) -> None:
        try:
            head = first + await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), 60)
        except (asyncio.LimitOverrunError, ValueError):
            await self._http_error(writer, "431 Request Header Fields Too Large")
            return
        if len(head) > HEAD_LIMIT:
            await self._http_error(writer, "431 Request Header Fields Too Large")
            return

        lines = head.split(b"\r\n")
        parts = lines[0].decode("latin-1").split(" ")
        if len(parts) != 3:
            await self._http_error(writer, "400 Bad Request")
            return
        method, target, version = parts
        headers = [line.decode("latin-1") for line in lines[1:] if line.strip()]

        if self.auth and not self._http_authorised(headers):
            LOG.warning("%s rejected: missing or bad Proxy-Authorization", who)
            await self._http_error(writer, "407 Proxy Authentication Required",
                                   extra='Proxy-Authenticate: Basic realm="htb"\r\n')
            return

        if method.upper() == "CONNECT":
            host, _, raw_port = target.rpartition(":")
            host = host or target
            port = int(raw_port) if raw_port.isdigit() else 443
            streams, failure = await self._open(host.strip("[]"), port)
            if failure:
                _, status, reason = failure
                LOG.info("connect %s -> %s:%d failed (%s)", who, host, port, reason)
                await self._http_error(writer, status)
                return
            remote_r, remote_w = streams
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            LOG.info("connect %s -> %s:%d", who, host, port)
            await _splice(reader, writer, remote_r, remote_w)
            return

        url = urlsplit(target)
        if not url.hostname:
            await self._http_error(writer, "400 Bad Request",
                                   body="This is a proxy; send an absolute URL or CONNECT.\n")
            return
        if url.scheme == "https":
            await self._http_error(writer, "400 Bad Request",
                                   body="Use CONNECT (or SOCKS5) for https.\n")
            return
        port = url.port or 80
        path = url.path or "/"
        if url.query:
            path = f"{path}?{url.query}"

        streams, failure = await self._open(url.hostname, port)
        if failure:
            _, status, reason = failure
            LOG.info("http %s -> %s:%d failed (%s)", who, url.hostname, port, reason)
            await self._http_error(writer, status)
            return
        remote_r, remote_w = streams

        kept = [h for h in headers if h.split(":", 1)[0].strip().lower() not in HOP_BY_HOP]
        if not any(h.lower().startswith("host:") for h in kept):
            kept.insert(0, f"Host: {url.netloc}")
        rebuilt = f"{method} {path} {version}\r\n" + "".join(f"{h}\r\n" for h in kept) \
                  + "Connection: close\r\n\r\n"
        remote_w.write(rebuilt.encode("latin-1"))
        await remote_w.drain()
        LOG.info("http %s -> %s %s:%d", who, method, url.hostname, port)
        await _splice(reader, writer, remote_r, remote_w)

    def _http_authorised(self, headers) -> bool:
        for header in headers:
            name, _, value = header.partition(":")
            if name.strip().lower() != "proxy-authorization":
                continue
            scheme, _, token = value.strip().partition(" ")
            if scheme.lower() != "basic":
                return False
            try:
                user, _, password = base64.b64decode(token).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return (user, password) == self.auth
        return False

    @staticmethod
    async def _http_error(writer, status, extra="", body="") -> None:
        payload = body.encode()
        writer.write(f"HTTP/1.1 {status}\r\n{extra}"
                     f"Content-Length: {len(payload)}\r\n"
                     f"Connection: close\r\n\r\n".encode("latin-1") + payload)
        try:
            await writer.drain()
        except OSError:
            pass


class Forwarder:
    """Plain TCP forwarder, used to expose the namespace proxy on localhost."""

    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.host, self.port, self.timeout = host, port, timeout

    async def __call__(self, reader, writer) -> None:
        try:
            remote_r, remote_w = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            LOG.warning("%s -> %s:%d unreachable: %s", _peer(writer), self.host,
                        self.port, exc)
            _close(writer)
            return
        await _splice(reader, writer, remote_r, remote_w)


async def serve(args) -> int:
    if args.forward:
        host, _, port = args.forward.rpartition(":")
        handler = Forwarder(host or "127.0.0.1", int(port), args.timeout)
        label = f"forwarding to {args.forward}"
    else:
        auth = None
        if args.auth:
            user, _, password = args.auth.partition(":")
            auth = (user, password)
        handler = Proxy(auth, args.timeout)
        label = "socks5 + http proxy" + (" (authenticated)" if auth else "")

    try:
        server = await asyncio.start_server(handler, args.listen, args.port,
                                            reuse_address=True)
    except OSError as exc:
        LOG.error("cannot listen on %s:%d - %s", args.listen, args.port, exc)
        return 1

    if args.pidfile:
        try:
            with open(args.pidfile, "w") as handle:
                handle.write(f"{os.getpid()}\n")
        except OSError as exc:
            LOG.warning("could not write %s: %s", args.pidfile, exc)

    LOG.info("listening on %s:%d - %s", args.listen, args.port, label)

    stop = asyncio.get_running_loop().create_future()
    for signame in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(
                signame, lambda: stop.done() or stop.set_result(None))
        except (NotImplementedError, RuntimeError):
            pass

    async with server:
        await stop
    LOG.info("shutting down")
    if args.pidfile:
        try:
            os.unlink(args.pidfile)
        except OSError:
            pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m htb.socks",
        description="SOCKS5/HTTP proxy for the htb-cli lab namespace.")
    parser.add_argument("--listen", default="127.0.0.1", help="address to bind")
    parser.add_argument("--port", type=int, default=1080, help="port to bind")
    parser.add_argument("--auth", metavar="USER:PASS", help="require these credentials")
    parser.add_argument("--forward", metavar="HOST:PORT",
                        help="act as a plain TCP forwarder instead of a proxy")
    parser.add_argument("--pidfile", help="write the pid here once listening")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="outbound connect timeout in seconds")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stderr)
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
