"""Loopback HTTP API and built frontend host (standard library only)."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import mimetypes
from pathlib import Path
import secrets
from urllib.parse import unquote, urlsplit

from .domain import ApiError
from .service import DeviceService


# Only for the explicitly started local development server; desktop uses a random token.
DEVELOPMENT_TOKEN = "upm-development-only"
MAX_BODY_BYTES = 16 * 1024
LOGGER = logging.getLogger(__name__)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        # Windows SO_REUSEADDR can allow a second server to hijack a bound port.
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(host="127.0.0.1", port=0, data_dir=None, token=None, static_dir=None, *,
                  mode="simulation", provider=None, automation_service=None, pick_file=None,
                  autostart=None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("The server must bind to IPv4 loopback.")
    if token is None:
        token = secrets.token_urlsafe(32)
    if not isinstance(token, str) or not token or not token.isascii():
        raise ValueError("Token must be a nonempty ASCII string.")
    service = DeviceService(data_dir, provider, mode=mode, automation_service=automation_service, pick_file=pick_file,
                            autostart=autostart)
    static_root = Path(static_dir).resolve() if static_dir is not None else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "UPM/1"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            # Never log URLs, which may contain shell bootstrap data.
            LOGGER.debug("HTTP request handled")

        def _json(self, status, payload):
            # Escape arbitrary input echoed in validation messages, including lone surrogates.
            content = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
            self._respond(status, content, "application/json; charset=utf-8")

        def _respond(self, status, content=b"", content_type="text/plain; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            origin = self.headers.get("Origin")
            if origin and origin in self._allowed_origins():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-UPM-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, PATCH, POST, PUT, DELETE, OPTIONS")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def _allowed_origins(self):
            own_port = self.server.server_address[1]
            origins = {f"http://127.0.0.1:{own_port}", f"http://localhost:{own_port}"}
            if static_root is None:
                origins.update({"http://localhost:5173", "http://127.0.0.1:5173"})
            return origins

        def _security(self, path, preflight=False):
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise ApiError(403, "forbidden", "Only loopback clients are permitted.")
            expected = {f"127.0.0.1:{self.server.server_address[1]}", f"localhost:{self.server.server_address[1]}"}
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0].lower() not in expected:
                raise ApiError(403, "invalid_host", "Invalid loopback Host header.")
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and origins[0] not in self._allowed_origins()):
                raise ApiError(403, "invalid_origin", "Origin is not permitted.")
            if path.startswith("/api/") and not preflight:
                supplied = self.headers.get_all("X-UPM-Token", [])
                if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode("utf-8"), token.encode("ascii")):
                    raise ApiError(401, "unauthorized", "A valid session token is required.")

        def _body(self, limit=MAX_BODY_BYTES):
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                raise ApiError(400, "invalid_body", "A single Content-Length is required.")
            try:
                size = int(lengths[0])
            except ValueError:
                raise ApiError(400, "invalid_body", "Invalid Content-Length.") from None
            if size < 0 or size > limit:
                raise ApiError(413, "body_too_large", "JSON body exceeds the permitted size.")
            types = self.headers.get_all("Content-Type", [])
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise ApiError(415, "invalid_content_type", "Content-Type must be application/json.")
            self._body_consumed = True
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ApiError(400, "invalid_json", "Incomplete JSON body.")
            def reject_constant(value):
                raise ValueError("Non-finite JSON number")
            try:
                return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise ApiError(400, "invalid_json", "Body must contain valid JSON.") from None

        def _drain_body(self):
            """Read an unread small request body before replying with an error.

            Closing a Windows socket with unread data resets the connection, so the
            client would see ConnectionAbortedError instead of the JSON error.
            """
            if getattr(self, "_body_consumed", False):
                return
            self._body_consumed = True
            lengths = self.headers.get_all("Content-Length", [])
            try:
                size = int(lengths[0]) if len(lengths) == 1 else 0
            except ValueError:
                size = 0
            if 0 < size <= 64 * 1024:
                self.rfile.read(size)
            elif size:
                self.close_connection = True

        def _dispatch(self):
            self._body_consumed = False
            try:
                parsed = urlsplit(self.path)
                if parsed.scheme or parsed.netloc:
                    raise ApiError(400, "invalid_path", "Absolute request targets are not supported.")
                path = unquote(parsed.path)
                self._security(path, self.command == "OPTIONS")
                if self.command == "OPTIONS":
                    if not path.startswith("/api/v1/"):
                        raise ApiError(404, "not_found", "Route not found.")
                    self._respond(204)
                    return
                parts = path.strip("/").split("/")
                if self.command == "GET" and path == "/api/v1/health":
                    self._json(200, {"status": "ok", "mode": service.mode, "apiVersion": 1,
                                     "devicesRevision": service.devices_revision})
                elif self.command == "GET" and path == "/api/v1/devices":
                    self._json(200, {"devices": service.devices()})
                elif self.command == "PATCH" and len(parts) == 5 and parts[:3] == ["api", "v1", "devices"] and parts[4] == "settings":
                    self._json(200, {"device": service.update(parts[3], self._body())})
                elif path == "/api/v1/profiles" and self.command == "GET":
                    self._json(200, {"profiles": service.profiles(), "profileState": service.profile_state()})
                elif path == "/api/v1/profiles" and self.command == "POST":
                    self._json(201, {"profile": service.create_profile(self._body()), "profileState": service.profile_state()})
                elif self.command == "POST" and len(parts) == 5 and parts[:3] == ["api", "v1", "profiles"] and parts[4] == "activate":
                    if self._body() != {}:
                        raise ApiError(400, "invalid_body", "Activation body must be an empty object.")
                    self._json(200, service.activate(parts[3]))
                elif self.command == "PATCH" and len(parts) == 4 and parts[:3] == ["api", "v1", "profiles"]:
                    self._json(200, {"profile": service.update_profile(parts[3], self._body()), "profileState": service.profile_state()})
                elif self.command == "DELETE" and len(parts) == 4 and parts[:3] == ["api", "v1", "profiles"]:
                    self._json(200, {"profiles": service.delete_profile(parts[3]), "profileState": service.profile_state()})
                elif self.command == "DELETE" and path == "/api/v1/data":
                    self._json(200, service.clear_data())
                elif self.command == "GET" and path == "/api/v1/preferences":
                    self._json(200, service.preferences())
                elif self.command == "PUT" and path == "/api/v1/preferences":
                    self._json(200, service.update_preferences(self._body()))
                elif self.command == "GET" and path == "/api/v1/assignments":
                    self._json(200, service.automation.state())
                elif self.command == "PUT" and len(parts) == 5 and parts[:3] == ["api", "v1", "assignments"]:
                    self._json(200, service.automation.save(parts[3], parts[4], self._body()))
                elif self.command == "POST" and path == "/api/v1/dialogs/open-file":
                    if self._body() != {}:
                        raise ApiError(400, "invalid_body", "Request body must be an empty object.")
                    self._json(200, service.automation.pick_file())
                elif self.command in ("GET", "HEAD") and not path.startswith("/api/"):
                    self._static(path)
                else:
                    raise ApiError(404, "not_found", "Route not found.")
            except ApiError as exc:
                self._drain_body()
                self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True
            except Exception:
                LOGGER.exception("Unhandled local API failure")
                self._json(500, {"error": {"code": "internal_error", "message": "The local service could not complete this request."}})

        def _static(self, path):
            if static_root is None:
                raise ApiError(404, "not_found", "Frontend build is not configured.")
            if "\\" in path or "\x00" in path or any(p in (".", "..") for p in path.split("/")) or ":" in path:
                raise ApiError(400, "invalid_path", "Invalid asset path.")
            candidate = (static_root / path.lstrip("/")).resolve()
            if not candidate.is_relative_to(static_root):
                raise ApiError(403, "forbidden", "Asset path is outside the frontend directory.")
            if path == "/":
                candidate = static_root / "index.html"
            elif not candidate.is_file() and not candidate.suffix and "text/html" in self.headers.get("Accept", ""):
                candidate = static_root / "index.html"
            candidate = candidate.resolve()
            if not candidate.is_relative_to(static_root):
                raise ApiError(403, "forbidden", "Asset path is outside the frontend directory.")
            if not candidate.is_file():
                raise ApiError(404, "not_found", "Asset not found.")
            content_type = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}.get(candidate.suffix.lower())
            content_type = content_type or mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self._respond(200, candidate.read_bytes(), content_type)

        do_GET = _dispatch
        do_HEAD = _dispatch
        do_POST = _dispatch
        do_PATCH = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch
        do_OPTIONS = _dispatch

    try:
        server = LocalServer((host, port), Handler)
    except BaseException:
        service.close()
        raise
    server.service = service
    server.api_token = token
    return server


def main():
    parser = argparse.ArgumentParser(description="Run the simulated UPM loopback development API.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with create_server(port=args.port, data_dir=args.data_dir, token=DEVELOPMENT_TOKEN) as server:
        print(f"Simulation API: http://127.0.0.1:{server.server_address[1]} (development token required)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
