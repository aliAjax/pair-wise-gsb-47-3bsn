"""设备周转台用例编排：权限检查、派单选择、待派区与可派清单组装。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, PermissionDenied, ValidationError
from .equipment_repository import EquipmentRepository
from .equipment_rules import EquipmentRules, MODEL_LABELS, parse_time, to_iso


class EquipmentService:
    def __init__(self, repository: EquipmentRepository, rules: EquipmentRules = None) -> None:
        self.repository = repository
        self.rules = rules or EquipmentRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure(self, actor: Actor, action: str) -> Actor:
        actor = self._actor(actor)
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问设备周转台")
        if not self.rules.role_can(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        return actor

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _device_view(self, device: Dict[str, Any], now: datetime, requests: List[Dict[str, Any]]) -> Dict[str, Any]:
        view = dict(device)
        view["model_label"] = MODEL_LABELS.get(device["model"], device["model"])
        view["disinfection_valid"] = self.rules.disinfection_valid(device, now)
        view["dispatchable"] = self.rules.dispatchable(device, now)
        back = self.rules.expected_return(device, requests)
        view["expected_return"] = to_iso(back) if back else None
        return view

    def _request_view(self, request: Dict[str, Any], devices: List[Dict[str, Any]], requests: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
        view = dict(request)
        view["model_label"] = MODEL_LABELS.get(request["model"], request["model"])
        # 待派申请显示它在等的设备编号（两家抢同一台时，后提交的一方能看见编号）
        view["waiting_for"] = None
        if request["state"] == "pending":
            view["waiting_for"] = self.rules.waiting_for(devices, requests, request, now)
        return view

    def register_device(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._ensure(actor, "register_device")
        prepared = self.rules.validate_device(payload)
        return self.repository.create_device(prepared, actor.user_id)

    def create_request(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._ensure(actor, "create_request")
        prepared = self.rules.validate_request(payload)
        return self.repository.create_request(prepared, actor.user_id)

    def dispatch(self, actor: Actor, request_id: int, device_code: Optional[str] = None) -> Dict[str, Any]:
        """派单：只选消毒有效且机型匹配的设备；没有可派设备时申请留在待派区。"""
        actor = self._ensure(actor, "dispatch")
        now = self._now()
        request = self.repository.get_request(request_id)
        self.rules.require_request_state(request, ["pending"], "dispatch")
        devices = self.repository.list_devices()
        if device_code:
            device = self.repository.get_device(device_code)
            if not self.rules.model_matches(device, request):
                raise ValidationError("机型不匹配：氧气驱动与涡轮不能混用")
            if not self.rules.dispatchable(device, now):
                raise Conflict("设备%s不在可派清单（待消毒或消毒已超期）" % device_code)
        else:
            device = self.rules.pick_device(devices, request, now)
            if device is None:
                waiting = self.rules.waiting_for(devices, self.repository.list_requests(), request, now)
                message = "暂无消毒有效且机型匹配的设备，申请留在待派区"
                if waiting is not None:
                    message += "，等待设备%s" % waiting
                raise Conflict(message)
        return self.repository.dispatch(request_id, device["code"], actor.user_id)

    def return_device(self, actor: Actor, device_code: str) -> Dict[str, Any]:
        actor = self._ensure(actor, "return")
        return self.repository.return_device(device_code, actor.user_id)

    def confirm_disinfection(self, actor: Actor, device_code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._ensure(actor, "disinfect")
        device = self.repository.get_device(device_code)
        now = self._now()
        # 待消毒设备必须确认后才能再派；在库但已超期的设备同样要重新确认消毒
        if device["state"] == "dispatched":
            raise Conflict("设备%s已派出，归还后才能消毒" % device_code)
        if device["state"] == "available" and self.rules.disinfection_valid(device, now):
            raise Conflict("设备%s消毒仍在有效期内，无需确认" % device_code)
        valid_until = to_iso(parse_time((payload or {}).get("disinfection_valid_until")))
        return self.repository.confirm_disinfection(device_code, valid_until, actor.user_id)

    def cancel_request(self, actor: Actor, request_id: int) -> Dict[str, Any]:
        actor = self._ensure(actor, "cancel_request")
        return self.repository.cancel_request(request_id, actor.user_id)

    def list_devices(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        now = self._now()
        requests = self.repository.list_requests()
        return [self._device_view(d, now, requests) for d in self.repository.list_devices()]

    def list_requests(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        now = self._now()
        devices = self.repository.list_devices()
        requests = self.repository.list_requests()
        views = [self._request_view(r, devices, requests, now) for r in requests]
        if state:
            views = [v for v in views if v["state"] == state]
        return views

    def board(self, actor: Actor) -> Dict[str, Any]:
        """周转台总览：可派清单、待派区、待消毒、已派出、超期设备分组展示。"""
        self._actor(actor)
        now = self._now()
        devices = self.repository.list_devices()
        requests = self.repository.list_requests()
        device_views = [self._device_view(d, now, requests) for d in devices]
        request_views = [self._request_view(r, devices, requests, now) for r in requests]
        dispatched_ids = {d["current_request_id"] for d in devices if d["current_request_id"] is not None}
        return {
            "now": to_iso(now),
            "dispatchable": [d for d in device_views if d["dispatchable"]],
            "pending_area": [r for r in request_views if r["state"] == "pending"],
            "pending_disinfection": [d for d in device_views if d["state"] == "pending_disinfection"],
            "dispatched": [d for d in device_views if d["state"] == "dispatched"],
            "expired": [d for d in device_views if d["state"] == "available" and not d["disinfection_valid"]],
            "active_requests": [r for r in request_views if r["state"] == "dispatched" or r["id"] in dispatched_ids],
            "devices": device_views,
            "requests": request_views,
        }

    def events(self, actor: Actor, entity: str, entity_ref: str) -> List[Dict[str, Any]]:
        self._actor(actor)
        if entity not in {"device", "request"}:
            raise ValidationError("entity只能是device/request")
        return self.repository.events(entity, entity_ref)
