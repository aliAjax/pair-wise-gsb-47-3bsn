"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
EQUIPMENT_REQUEST_RE = re.compile(r"^/api/equipment/requests/(\d+)/(dispatch|cancel)$")
EQUIPMENT_DEVICE_RE = re.compile(r"^/api/equipment/devices/([A-Za-z0-9_-]+)/(return|disinfect)$")
EQUIPMENT_EVENTS_RE = re.compile(r"^/api/equipment/events/(device|request)/([^/]+)$")


def make_handler(service: Any, static_dir: Path, equipment: Any = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hospital-surge/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "hospital-surge", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/equipment":
                    page = (static_dir / "equipment.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/equipment/board" and equipment is not None:
                    self._send(200, equipment.board(self._actor()))
                    return
                if parsed.path == "/api/equipment/devices" and equipment is not None:
                    self._send(200, {"items": equipment.list_devices(self._actor())})
                    return
                if parsed.path == "/api/equipment/requests" and equipment is not None:
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": equipment.list_requests(self._actor(), state=query.get("state", [None])[0])})
                    return
                match = EQUIPMENT_EVENTS_RE.match(parsed.path)
                if match and equipment is not None:
                    self._send(200, {"items": equipment.events(self._actor(), match.group(1), match.group(2))})
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                if parsed.path == "/api/equipment/devices" and equipment is not None:
                    self._send(201, equipment.register_device(self._actor(), body))
                    return
                if parsed.path == "/api/equipment/requests" and equipment is not None:
                    self._send(201, equipment.create_request(self._actor(), body))
                    return
                match = EQUIPMENT_REQUEST_RE.match(parsed.path)
                if match and equipment is not None:
                    if match.group(2) == "dispatch":
                        self._send(200, equipment.dispatch(self._actor(), int(match.group(1)), device_code=body.get("device_code")))
                    else:
                        self._send(200, equipment.cancel_request(self._actor(), int(match.group(1))))
                    return
                match = EQUIPMENT_DEVICE_RE.match(parsed.path)
                if match and equipment is not None:
                    if match.group(2) == "return":
                        self._send(200, equipment.return_device(self._actor(), match.group(1)))
                    else:
                        self._send(200, equipment.confirm_disinfection(self._actor(), match.group(1), body))
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path, equipment: Any = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir, equipment))
