#!/usr/bin/env python3
"""Simple HTTP/HTTPS forward proxy server.

Supports:
- HTTP forward proxy (GET, POST, PUT, DELETE, HEAD, OPTIONS, PATCH)
- HTTPS CONNECT tunnel
- Configuration via environment variables
- Logging to stdout
- Graceful shutdown
"""

import http.client
import logging
import os
import select
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_PORT = int(os.environ.get("PROXY_PORT") or os.environ.get("PORT") or "1111")
PROXY_BIND = os.environ.get("PROXY_BIND") or "0.0.0.0"
PROXY_LOG = (os.environ.get("PROXY_LOG") or "true").lower() in ("true", "1", "yes")
KEEP_ALIVE = (os.environ.get("KEEP_ALIVE") or "false").lower() in ("true", "1", "yes")
KEEP_ALIVE_INTERVAL = int(os.environ.get("KEEP_ALIVE_INTERVAL") or "120")  # seconds

TIMEOUT = 60  # seconds
BUFFER_SIZE = 65536

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("proxy")
logger.setLevel(logging.INFO if PROXY_LOG else logging.WARNING)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Proxy handler
# ---------------------------------------------------------------------------


class ProxyHandler(BaseHTTPRequestHandler):
    """Handles individual proxy requests."""

    server_version = "ForwardProxy/1.0"

    # Suppress default BaseHTTPRequestHandler logging — we do our own.
    def log_message(self, format, *args):  # noqa: A002
        pass

    # ---- HTTPS CONNECT tunnel ------------------------------------------

    def do_CONNECT(self):
        host, port = self._parse_host_port(self.path, default_port=443)
        try:
            remote = socket.create_connection((host, port), timeout=TIMEOUT)
        except Exception as e:
            self.send_error(502, f"Cannot connect to {host}:{port} — {e}")
            logger.info("CONNECT  %s:%s  502 (%s)", host, port, e)
            return

        self.send_response_only(200, "Connection Established")
        self.end_headers()
        logger.info("CONNECT  %s:%s  200", host, port)

        self._tunnel(self.connection, remote)

    # ---- HTTP methods --------------------------------------------------

    def do_GET(self):
        self._proxy_request()

    def do_POST(self):
        self._proxy_request()

    def do_PUT(self):
        self._proxy_request()

    def do_DELETE(self):
        self._proxy_request()

    def do_HEAD(self):
        self._proxy_request()

    def do_OPTIONS(self):
        self._proxy_request()

    def do_PATCH(self):
        self._proxy_request()

    # ---- Internal helpers ----------------------------------------------

    def _proxy_request(self):
        url = urllib.parse.urlsplit(self.path)
        host, port = self._parse_host_port(url.netloc, default_port=80)

        # Build the path that the origin server expects.
        path = url.path or "/"
        if url.query:
            path = f"{path}?{url.query}"

        # Read body if present.
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else None

        # Forward headers, dropping hop-by-hop ones.
        headers = {}
        hop_by_hop = {
            "proxy-connection",
            "keep-alive",
            "transfer-encoding",
            "te",
            "connection",
            "proxy-authorization",
            "proxy-authenticate",
            "upgrade",
        }
        for key, value in self.headers.items():
            if key.lower() not in hop_by_hop:
                headers[key] = value

        try:
            conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
            conn.request(self.command, path, body=body, headers=headers)
            response = conn.getresponse()
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")
            logger.info("%-8s %s  502 (%s)", self.command, self.path, e)
            return

        # Send status line.
        self.send_response_only(response.status, response.reason)

        # Forward response headers.
        for key, value in response.getheaders():
            if key.lower() not in ("transfer-encoding",):
                self.send_header(key, value)
        self.end_headers()

        # Stream the body back.
        try:
            while True:
                chunk = response.read(BUFFER_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

        logger.info("%-8s %s  %s", self.command, self.path, response.status)

    def _tunnel(self, client_sock: socket.socket, remote_sock: socket.socket):
        """Bidirectional relay between *client_sock* and *remote_sock*."""
        sockets = [client_sock, remote_sock]
        client_sock.settimeout(0)
        remote_sock.settimeout(0)

        try:
            while True:
                readable, _, errors = select.select(sockets, [], sockets, TIMEOUT)
                if errors:
                    break
                if not readable:
                    break  # timeout
                for sock in readable:
                    other = remote_sock if sock is client_sock else client_sock
                    try:
                        data = sock.recv(BUFFER_SIZE)
                    except (ConnectionResetError, OSError):
                        data = b""
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return
        finally:
            remote_sock.close()

    @staticmethod
    def _parse_host_port(address: str, default_port: int = 80) -> tuple[str, int]:
        if ":" in address:
            host, port_str = address.rsplit(":", 1)
            try:
                return host, int(port_str)
            except ValueError:
                return address, default_port
        return address, default_port


# ---------------------------------------------------------------------------
# Threaded server
# ---------------------------------------------------------------------------


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _keep_alive_loop():
    """Periodically pings an external endpoint to prevent the container from sleeping."""
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            conn = http.client.HTTPSConnection("httpbin.org", timeout=10)
            conn.request("GET", "/status/200")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            logger.info("KEEPALIVE  httpbin.org/status/200  %s", resp.status)
        except Exception as e:
            logger.info("KEEPALIVE  httpbin.org/status/200  failed (%s)", e)


def main():
    server = ProxyServer((PROXY_BIND, PROXY_PORT), ProxyHandler)

    def _shutdown(signum, frame):
        logger.info("Shutting down…")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if KEEP_ALIVE:
        t = threading.Thread(target=_keep_alive_loop, daemon=True)
        t.start()
        logger.info("Keep-alive enabled (every %ss)", KEEP_ALIVE_INTERVAL)

    logger.info("Proxy listening on %s:%s", PROXY_BIND, PROXY_PORT)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        logger.info("Proxy stopped.")


if __name__ == "__main__":
    main()
