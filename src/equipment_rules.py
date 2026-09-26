"""设备周转台规则：机型匹配、消毒有效期、派单选择与待派冲突。

只放规则，不碰资料（equipment_repository）和页面（static/equipment.html）。
"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .domain import Conflict, ValidationError, choice, number, text


# 机型：氧气驱动与涡轮不能混用，只有同机型才算匹配
MODELS = ["oxygen_driven", "turbine"]
MODEL_LABELS = {"oxygen_driven": "氧气驱动", "turbine": "涡轮"}

DEVICE_STATES = ["available", "dispatched", "pending_disinfection"]
REQUEST_STATES = ["pending", "dispatched", "returned", "cancelled"]

# 设备管理员负责登记、派单、消毒确认；科室用户负责申请、归还、取消
ACTION_ROLES = {
    "register_device": {"equipment_keeper"},
    "create_request": {"department_user", "equipment_keeper"},
    "dispatch": {"equipment_keeper"},
    "return": {"department_user", "equipment_keeper"},
    "disinfect": {"equipment_keeper"},
    "cancel_request": {"department_user", "equipment_keeper"},
}


def parse_time(value: Any, key: str = "disinfection_valid_until") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    raw = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("%s必须是ISO时间" % key) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class EquipmentRules:
    def known_role(self, role: str) -> bool:
        all_roles = set()
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_device(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        p["code"] = text(p, "code")
        p["model"] = choice(p, "model", MODELS)
        p["disinfection_valid_until"] = to_iso(parse_time(p.get("disinfection_valid_until")))
        return p

    def validate_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        p["requester"] = text(p, "requester")
        p["model"] = choice(p, "model", MODELS)
        p["destination"] = text(p, "destination")
        duration = number(p, "duration_hours", 0)
        if duration <= 0:
            raise ValidationError("duration_hours必须大于0")
        p["duration_hours"] = duration
        return p

    def disinfection_valid(self, device: Dict[str, Any], now: datetime) -> bool:
        return parse_time(device["disinfection_valid_until"]) > now

    def dispatchable(self, device: Dict[str, Any], now: datetime) -> bool:
        """可派 = 在库 + 消毒在有效期内；待消毒、已派出、超期一律不进可派清单。"""
        return device["state"] == "available" and self.disinfection_valid(device, now)

    def model_matches(self, device: Dict[str, Any], request: Dict[str, Any]) -> bool:
        """氧气驱动与涡轮机型不能混用。"""
        return device["model"] == request["model"]

    def covers_duration(self, device: Dict[str, Any], request: Dict[str, Any], now: datetime) -> bool:
        """消毒有效期是否覆盖申请预计时长。"""
        if not self.disinfection_valid(device, now):
            return False
        need = now.timestamp() + float(request["duration_hours"]) * 3600
        return parse_time(device["disinfection_valid_until"]).timestamp() >= need

    def pick_device(self, devices: Iterable[Dict[str, Any]], request: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
        """派单选机：机型匹配 + 消毒有效；优先能覆盖预计时长的，其中先用最快到期的。"""
        candidates = [d for d in devices if self.dispatchable(d, now) and self.model_matches(d, request)]
        covering = [d for d in candidates if self.covers_duration(d, request, now)]
        pool = covering or candidates
        if not pool:
            return None
        return sorted(pool, key=lambda d: parse_time(d["disinfection_valid_until"]))[0]

    def expected_return(self, device: Dict[str, Any], requests: Iterable[Dict[str, Any]]) -> Optional[datetime]:
        """已派出设备的预计归还时刻 = 派出时刻 + 申请预计时长。"""
        current = device.get("current_request_id")
        if current is None:
            return None
        for req in requests:
            if req["id"] == current and req.get("dispatched_at"):
                moment = parse_time(req["dispatched_at"], "dispatched_at")
                return datetime.fromtimestamp(moment.timestamp() + float(req["duration_hours"]) * 3600, tz=timezone.utc)
        return None

    def waiting_for(self, devices: Iterable[Dict[str, Any]], requests: Iterable[Dict[str, Any]], request: Dict[str, Any], now: datetime) -> Optional[str]:
        """申请排不到设备时它在等的设备编号：同机型但暂不可派的设备。

        两家申请同一台时，后提交的申请通过该编号知道自己排在哪台设备后面。
        排序：已派出（按预计归还先后）→ 待消毒 → 在库但超期。
        """
        busy = [d for d in devices if self.model_matches(d, request) and not self.dispatchable(d, now)]
        if not busy:
            return None

        def sort_key(device: Dict[str, Any]):
            if device["state"] == "dispatched":
                back = self.expected_return(device, requests)
                return (0, back.timestamp() if back else float("inf"))
            if device["state"] == "pending_disinfection":
                return (1, 0.0)
            return (2, 0.0)

        return sorted(busy, key=sort_key)[0]["code"]

    def require_device_state(self, device: Dict[str, Any], allowed: List[str], action: str) -> None:
        if device["state"] not in allowed:
            raise Conflict("设备%s当前状态不允许%s" % (device["code"], action))

    def require_request_state(self, request: Dict[str, Any], allowed: List[str], action: str) -> None:
        if request["state"] not in allowed:
            raise Conflict("申请%s当前状态不允许%s" % (request["id"], action))
