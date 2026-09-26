"""设备周转台资料层：SQLite 表结构与事务访问。

设备、申请、周转事件分表保存；派单/归还/消毒确认在单事务内完成并校验状态，
避免两批人同时抢到同一台设备。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


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
                CREATE TABLE IF NOT EXISTS equipment_devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL,
                    disinfection_valid_until TEXT NOT NULL,
                    current_request_id INTEGER,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requester TEXT NOT NULL,
                    model TEXT NOT NULL,
                    duration_hours REAL NOT NULL,
                    destination TEXT NOT NULL,
                    state TEXT NOT NULL,
                    device_code TEXT,
                    dispatched_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity TEXT NOT NULL,
                    entity_ref TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_equipment_devices_state ON equipment_devices(state);
                CREATE INDEX IF NOT EXISTS idx_equipment_requests_state ON equipment_requests(state);
                CREATE INDEX IF NOT EXISTS idx_equipment_events_ref ON equipment_events(entity, entity_ref, id);
                """
            )

    @staticmethod
    def _device(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _request(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _log(self, connection: sqlite3.Connection, entity: str, entity_ref: str, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO equipment_events(entity,entity_ref,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (entity, entity_ref, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    def create_device(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO equipment_devices(code,model,state,disinfection_valid_until,current_request_id,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (payload["code"], payload["model"], "available", payload["disinfection_valid_until"], None, 1, actor_id, actor_id, now, now),
                )
                self._log(connection, "device", payload["code"], "registered", actor_id, {"model": payload["model"], "disinfection_valid_until": payload["disinfection_valid_until"]})
                row = connection.execute("SELECT * FROM equipment_devices WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("设备编号已存在") from exc
        return self._device(row)

    def get_device(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM equipment_devices WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("设备不存在")
        return self._device(row)

    def list_devices(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM equipment_devices ORDER BY code").fetchall()
        return [self._device(row) for row in rows]

    def create_request(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO equipment_requests(requester,model,duration_hours,destination,state,device_code,dispatched_at,version,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (payload["requester"], payload["model"], payload["duration_hours"], payload["destination"], "pending", None, None, 1, actor_id, now, now),
            )
            request_id = int(cursor.lastrowid)
            self._log(connection, "request", str(request_id), "created", actor_id, {"requester": payload["requester"], "model": payload["model"], "destination": payload["destination"]})
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (request_id,)).fetchone()
        return self._request(row)

    def get_request(self, request_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (int(request_id),)).fetchone()
        if row is None:
            raise NotFound("申请不存在")
        return self._request(row)

    def list_requests(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM equipment_requests WHERE state=? ORDER BY id", (state,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM equipment_requests ORDER BY id").fetchall()
        return [self._request(row) for row in rows]

    def dispatch(self, request_id: int, device_code: str, actor_id: str) -> Dict[str, Any]:
        """派单：申请与设备在同一事务内翻转状态，状态不符即冲突，防止重复派台。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            req = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (int(request_id),)).fetchone()
            dev = connection.execute("SELECT * FROM equipment_devices WHERE code=?", (device_code,)).fetchone()
            if req is None or dev is None:
                connection.rollback()
                raise NotFound("申请或设备不存在")
            if req["state"] != "pending":
                connection.rollback()
                raise Conflict("申请已被处理，请刷新待派区")
            if dev["state"] != "available":
                connection.rollback()
                raise Conflict("设备%s已被派出或待消毒" % device_code)
            connection.execute(
                "UPDATE equipment_requests SET state='dispatched',device_code=?,dispatched_at=?,version=version+1,updated_at=? WHERE id=?",
                (device_code, now, now, int(request_id)),
            )
            connection.execute(
                "UPDATE equipment_devices SET state='dispatched',current_request_id=?,version=version+1,updated_by=?,updated_at=? WHERE code=?",
                (int(request_id), actor_id, now, device_code),
            )
            self._log(connection, "request", str(request_id), "dispatched", actor_id, {"device_code": device_code})
            self._log(connection, "device", device_code, "dispatched", actor_id, {"request_id": int(request_id), "destination": req["destination"]})
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (int(request_id),)).fetchone()
            connection.commit()
        return self._request(row)

    def return_device(self, device_code: str, actor_id: str) -> Dict[str, Any]:
        """归还：设备转待消毒，对应申请结单；确认消毒前不得再次派单。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            dev = connection.execute("SELECT * FROM equipment_devices WHERE code=?", (device_code,)).fetchone()
            if dev is None:
                connection.rollback()
                raise NotFound("设备不存在")
            if dev["state"] != "dispatched":
                connection.rollback()
                raise Conflict("设备%s不在派出状态" % device_code)
            request_id = dev["current_request_id"]
            connection.execute(
                "UPDATE equipment_devices SET state='pending_disinfection',current_request_id=NULL,version=version+1,updated_by=?,updated_at=? WHERE code=?",
                (actor_id, now, device_code),
            )
            if request_id is not None:
                connection.execute(
                    "UPDATE equipment_requests SET state='returned',version=version+1,updated_at=? WHERE id=?",
                    (now, int(request_id)),
                )
                self._log(connection, "request", str(request_id), "returned", actor_id, {"device_code": device_code})
            self._log(connection, "device", device_code, "returned", actor_id, {"state": "pending_disinfection"})
            row = connection.execute("SELECT * FROM equipment_devices WHERE code=?", (device_code,)).fetchone()
            connection.commit()
        return self._device(row)

    def confirm_disinfection(self, device_code: str, valid_until: str, actor_id: str) -> Dict[str, Any]:
        """消毒确认：写入新的消毒有效期，设备回到可派状态。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            dev = connection.execute("SELECT * FROM equipment_devices WHERE code=?", (device_code,)).fetchone()
            if dev is None:
                connection.rollback()
                raise NotFound("设备不存在")
            connection.execute(
                "UPDATE equipment_devices SET state='available',disinfection_valid_until=?,version=version+1,updated_by=?,updated_at=? WHERE code=?",
                (valid_until, actor_id, now, device_code),
            )
            self._log(connection, "device", device_code, "disinfected", actor_id, {"disinfection_valid_until": valid_until})
            row = connection.execute("SELECT * FROM equipment_devices WHERE code=?", (device_code,)).fetchone()
            connection.commit()
        return self._device(row)

    def cancel_request(self, request_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            req = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (int(request_id),)).fetchone()
            if req is None:
                connection.rollback()
                raise NotFound("申请不存在")
            if req["state"] != "pending":
                connection.rollback()
                raise Conflict("只有待派申请可以取消")
            connection.execute(
                "UPDATE equipment_requests SET state='cancelled',version=version+1,updated_at=? WHERE id=?",
                (now, int(request_id)),
            )
            self._log(connection, "request", str(request_id), "cancelled", actor_id, {})
            row = connection.execute("SELECT * FROM equipment_requests WHERE id=?", (int(request_id),)).fetchone()
            connection.commit()
        return self._request(row)

    def events(self, entity: str, entity_ref: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM equipment_events WHERE entity=? AND entity_ref=? ORDER BY id",
                (entity, entity_ref),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
