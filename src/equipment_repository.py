"""设备周转台 SQLite 表结构与事务访问（资料独立保存）。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound
from .equipment_rules import EquipmentUnavailable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EquipmentRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    model_type TEXT NOT NULL,
                    disinfection_valid_until TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    equipment_code TEXT,
                    waiting_equipment_code TEXT,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_type TEXT NOT NULL,
                    subject_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_equipment_state ON equipment(state);
                CREATE INDEX IF NOT EXISTS idx_equipment_requests_state ON equipment_requests(state);
                CREATE INDEX IF NOT EXISTS idx_equipment_events_subject ON equipment_events(subject_type, subject_id, id);
                """
            )

    @staticmethod
    def _event(connection: sqlite3.Connection, subject_type: str, subject_id: int, action: str, actor_id: str, version: int, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO equipment_events(subject_type,subject_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (subject_type, subject_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    @staticmethod
    def _request_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create_equipment(self, code: str, model_type: str, disinfection_valid_until: str, actor_id: str, state: str = "available") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO equipment(code,model_type,disinfection_valid_until,state,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (code, model_type, disinfection_valid_until, state, 1, actor_id, actor_id, now, now),
                )
                equipment_id = int(cursor.lastrowid)
                self._event(connection, "equipment", equipment_id, "registered", actor_id, 1, {"state": state})
                row = connection.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("设备编号已存在") from exc
        return dict(row)

    def get_equipment(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("设备不存在")
        return dict(row)

    def list_equipment(self, state: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM equipment WHERE state=? ORDER BY id LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM equipment ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def create_request(self, reference: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        waiting = payload.get("desired_equipment_code") or None
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO equipment_requests(reference,state,version,payload,equipment_code,waiting_equipment_code,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (reference, "pending", 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), None, waiting, actor_id, actor_id, now, now),
                )
                request_id = int(cursor.lastrowid)
                self._event(connection, "request", request_id, "created", actor_id, 1, {"state": "pending"})
                row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._request_row(row)

    def get_request(self, request_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise NotFound("申请不存在")
        return self._request_row(row)

    def list_requests(self, state: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM equipment_requests WHERE state=? ORDER BY id LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM equipment_requests ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [self._request_row(row) for row in rows]

    def assign_device(self, request_id: int, request_version: int, equipment_id: int, equipment_version: int, request_payload: Dict[str, Any], expected_return_at: str, min_valid_until: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
            equipment = connection.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
            if request is None or equipment is None:
                connection.rollback()
                raise NotFound("申请或设备不存在")
            if int(request["version"]) != int(request_version) or int(equipment["version"]) != int(equipment_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if request["state"] != "pending":
                connection.rollback()
                raise Conflict("申请不在待派区")
            if equipment["state"] != "available":
                connection.rollback()
                raise EquipmentUnavailable("设备已被先提交的申请派出")
            if str(equipment["disinfection_valid_until"]) <= str(min_valid_until):
                connection.rollback()
                raise Conflict("设备消毒已超期，不能派单")
            next_request_version = int(request_version) + 1
            next_equipment_version = int(equipment["version"]) + 1
            connection.execute(
                "UPDATE equipment SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                ("dispatched", next_equipment_version, actor_id, now, equipment_id),
            )
            connection.execute(
                "UPDATE equipment_requests SET state=?,version=?,payload=?,equipment_code=?,waiting_equipment_code=NULL,updated_by=?,updated_at=? WHERE id=?",
                ("dispatched", next_request_version, json.dumps(request_payload, ensure_ascii=False, sort_keys=True), equipment["code"], actor_id, now, request_id),
            )
            self._event(connection, "equipment", equipment_id, "dispatched", actor_id, next_equipment_version, {"request_reference": request["reference"], "destination": request_payload.get("destination"), "expected_return_at": expected_return_at})
            self._event(connection, "request", request_id, "dispatched", actor_id, next_request_version, {"equipment_code": equipment["code"], "expected_return_at": expected_return_at})
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
            connection.commit()
        return self._request_row(row)

    def defer_request(self, request_id: int, expected_version: int, waiting_code: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
            if request is None:
                connection.rollback()
                raise NotFound("申请不存在")
            if int(request["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if request["state"] != "pending":
                connection.rollback()
                raise Conflict("申请不在待派区")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE equipment_requests SET waiting_equipment_code=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (waiting_code, version, actor_id, now, request_id),
            )
            self._event(connection, "request", request_id, "dispatch_deferred", actor_id, version, {"equipment_code": waiting_code})
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
            connection.commit()
        return self._request_row(row)

    def return_equipment(self, code: str, expected_version: int, actor_id: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            equipment = connection.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
            if equipment is None:
                connection.rollback()
                raise NotFound("设备不存在")
            if int(equipment["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if equipment["state"] != "dispatched":
                connection.rollback()
                raise Conflict("设备不在派出状态")
            equipment_version = int(equipment["version"]) + 1
            connection.execute(
                "UPDATE equipment SET state=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                ("pending_disinfection", equipment_version, actor_id, now, equipment["id"]),
            )
            self._event(connection, "equipment", equipment["id"], "returned", actor_id, equipment_version, {"state": "pending_disinfection"})
            request = connection.execute(
                "SELECT * FROM equipment_requests WHERE equipment_code=? AND state='dispatched' ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
            request_row = None
            if request is not None:
                payload = json.loads(request["payload"])
                payload["returned_at"] = now
                request_version = int(request["version"]) + 1
                connection.execute(
                    "UPDATE equipment_requests SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    ("fulfilled", request_version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, request["id"]),
                )
                self._event(connection, "request", request["id"], "fulfilled", actor_id, request_version, {"equipment_code": code})
                request_row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request["id"],)).fetchone()
            equipment_row = connection.execute("SELECT * FROM equipment WHERE id=?", (equipment["id"],)).fetchone()
            connection.commit()
        return dict(equipment_row), (self._request_row(request_row) if request_row is not None else None)

    def confirm_disinfection(self, code: str, expected_version: int, disinfection_valid_until: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            equipment = connection.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
            if equipment is None:
                connection.rollback()
                raise NotFound("设备不存在")
            if int(equipment["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if equipment["state"] == "dispatched":
                connection.rollback()
                raise Conflict("设备已派出，需先归还")
            version = int(equipment["version"]) + 1
            connection.execute(
                "UPDATE equipment SET state=?,disinfection_valid_until=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                ("available", disinfection_valid_until, version, actor_id, now, equipment["id"]),
            )
            self._event(connection, "equipment", equipment["id"], "disinfection_confirmed", actor_id, version, {"disinfection_valid_until": disinfection_valid_until})
            row = connection.execute("SELECT * FROM equipment WHERE id=?", (equipment["id"],)).fetchone()
            connection.commit()
        return dict(row)

    def cancel_request(self, request_id: int, expected_version: int, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
            if request is None:
                connection.rollback()
                raise NotFound("申请不存在")
            if int(request["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if request["state"] != "pending":
                connection.rollback()
                raise Conflict("仅待派申请可取消")
            payload = json.loads(request["payload"])
            if reason:
                payload["cancel_reason"] = reason
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE equipment_requests SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                ("cancelled", version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, request_id),
            )
            self._event(connection, "request", request_id, "cancelled", actor_id, version, {"cancel_reason": reason})
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
            connection.commit()
        return self._request_row(row)

    def timeline(self, subject_type: str, subject_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM equipment_events WHERE subject_type=? AND subject_id=? ORDER BY id",
                (subject_type, subject_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, Dict[str, int]]:
        with self._connect() as connection:
            equipment_rows = connection.execute("SELECT state, COUNT(*) AS total FROM equipment GROUP BY state").fetchall()
            request_rows = connection.execute("SELECT state, COUNT(*) AS total FROM equipment_requests GROUP BY state").fetchall()
        return {
            "equipment": {str(row["state"]): int(row["total"]) for row in equipment_rows},
            "requests": {str(row["state"]): int(row["total"]) for row in request_rows},
        }
