"""设备周转台领域规则：设备状态机、派单筛选与申请校验（规则独立保存）。"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, ValidationError, choice, integer, optional_text, text


MODEL_TYPES = ["oxygen_driven", "turbine"]

STATE_AVAILABLE = "available"
STATE_DISPATCHED = "dispatched"
STATE_PENDING_DISINFECTION = "pending_disinfection"
EQUIPMENT_STATES = [STATE_AVAILABLE, STATE_DISPATCHED, STATE_PENDING_DISINFECTION]

REQUEST_PENDING = "pending"
REQUEST_DISPATCHED = "dispatched"
REQUEST_FULFILLED = "fulfilled"
REQUEST_CANCELLED = "cancelled"
REQUEST_STATES = [REQUEST_PENDING, REQUEST_DISPATCHED, REQUEST_FULFILLED, REQUEST_CANCELLED]

ACTION_ROLES = {
    "register": {"equipment_manager"},
    "return": {"equipment_manager"},
    "confirm_disinfection": {"equipment_manager"},
    "dispatch": {"equipment_manager"},
    "create_request": {"equipment_manager", "department_requester"},
    "cancel_request": {"equipment_manager", "department_requester"},
}

MAX_ESTIMATED_HOURS = 24 * 30
CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class EquipmentUnavailable(Conflict):
    """目标设备已被其他申请派出，申请留在待派区。"""

    code = "equipment_unavailable"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_instant(value: Any, key: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO日期时间" % key) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class EquipmentRules:
    EQUIPMENT_STATES = EQUIPMENT_STATES
    REQUEST_STATES = REQUEST_STATES

    def known_role(self, role: str) -> bool:
        if role == "admin":
            return True
        return any(role in roles for roles in ACTION_ROLES.values())

    def role_can(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_equipment(self, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        data = dict(payload or {})
        code = text(data, "code")
        if not CODE_RE.match(code):
            raise ValidationError("设备编号只能包含字母、数字、连字符和下划线")
        model_type = choice(data, "model_type", MODEL_TYPES)
        valid_until = parse_instant(data.get("disinfection_valid_until"), "disinfection_valid_until")
        if valid_until <= now:
            raise ValidationError("消毒有效期必须晚于当前时间")
        return {"code": code, "model_type": model_type, "disinfection_valid_until": format_instant(valid_until)}

    def validate_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        return {
            "requester": text(data, "requester"),
            "destination": text(data, "destination"),
            "model_type": choice(data, "model_type", MODEL_TYPES),
            "estimated_hours": integer(data, "estimated_hours", 1, MAX_ESTIMATED_HOURS),
            "desired_equipment_code": optional_text(data, "desired_equipment_code"),
        }

    def validate_disinfection(self, payload: Dict[str, Any], now: datetime) -> str:
        data = dict(payload or {})
        valid_until = parse_instant(data.get("disinfection_valid_until"), "disinfection_valid_until")
        if valid_until <= now:
            raise ValidationError("消毒有效期必须晚于当前时间")
        return format_instant(valid_until)

    def is_expired(self, equipment: Dict[str, Any], now: datetime) -> bool:
        return parse_instant(equipment["disinfection_valid_until"], "disinfection_valid_until") <= now

    def dispatchable(self, equipment: Dict[str, Any], now: datetime) -> bool:
        return equipment["state"] == STATE_AVAILABLE and not self.is_expired(equipment, now)

    def ensure_dispatchable(self, equipment: Dict[str, Any], model_type: str, now: datetime) -> None:
        if equipment["model_type"] != model_type:
            raise ValidationError("机型不匹配：氧气驱动和涡轮机型不能混用")
        if self.is_expired(equipment, now):
            raise Conflict("设备消毒已超期，不能派单")
        if equipment["state"] == STATE_PENDING_DISINFECTION:
            raise Conflict("设备待消毒，确认后才能派单")
        if equipment["state"] != STATE_AVAILABLE:
            raise EquipmentUnavailable("设备已被先提交的申请派出")

    def select_device(self, candidates: List[Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
        usable = [item for item in candidates if self.dispatchable(item, now)]
        if not usable:
            return None
        return sorted(usable, key=lambda item: (item["disinfection_valid_until"], item["code"]))[0]
