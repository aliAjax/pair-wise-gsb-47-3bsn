"""设备周转台用例编排：登记、申请、派单、归还与消毒确认。"""
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, PermissionDenied, optional_text, text
from .equipment_repository import EquipmentRepository
from .equipment_rules import REQUEST_PENDING, EquipmentRules, EquipmentUnavailable, format_instant, utc_now


class EquipmentService:
    def __init__(self, repository: EquipmentRepository, rules: EquipmentRules = None) -> None:
        self.repository = repository
        self.rules = rules or EquipmentRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    @staticmethod
    def _data(data: Any) -> Dict[str, Any]:
        return dict(data) if isinstance(data, dict) else {}

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_action(self, actor: Actor, action: str) -> None:
        self._ensure_known_role(actor)
        if not self.rules.role_can(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")

    def _enrich_equipment(self, item: Dict[str, Any], now) -> Dict[str, Any]:
        enriched = dict(item)
        enriched["expired"] = self.rules.is_expired(item, now)
        enriched["dispatchable"] = self.rules.dispatchable(item, now)
        return enriched

    def register_equipment(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_action(actor, "register")
        prepared = self.rules.validate_equipment(self._data(payload), utc_now())
        return self.repository.create_equipment(prepared["code"], prepared["model_type"], prepared["disinfection_valid_until"], actor.user_id)

    def list_equipment(self, actor: Actor, state: Optional[str] = None, dispatchable_only: bool = False) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        now = utc_now()
        items = [self._enrich_equipment(item, now) for item in self.repository.list_equipment(state=state)]
        if dispatchable_only:
            items = [item for item in items if item["dispatchable"]]
        return items

    def get_equipment(self, actor: Actor, code: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._enrich_equipment(self.repository.get_equipment(code), utc_now())

    def create_request(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_action(actor, "create_request")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.validate_request(self._data(payload))
        return self.repository.create_request(reference, prepared, actor.user_id)

    def list_requests(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_requests(state=state)

    def get_request(self, actor: Actor, request_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_request(request_id)

    def dispatch(self, actor: Actor, request_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_action(actor, "dispatch")
        request = self.repository.get_request(request_id)
        if request["state"] != REQUEST_PENDING:
            raise Conflict("申请不在待派区")
        data = self._data(data)
        target = data.get("equipment_code")
        target = target.strip() if isinstance(target, str) else ""
        if not target:
            target = request["payload"].get("desired_equipment_code") or ""
        now = utc_now()
        if target:
            equipment = self.repository.get_equipment(target)
            try:
                self.rules.ensure_dispatchable(equipment, request["payload"]["model_type"], now)
            except EquipmentUnavailable:
                return self.repository.defer_request(request_id, int(expected_version), target, actor.user_id)
            try:
                return self._assign(request, equipment, int(expected_version), now, actor)
            except EquipmentUnavailable:
                return self.repository.defer_request(request_id, int(expected_version), target, actor.user_id)
        model_type = request["payload"]["model_type"]
        for _ in range(3):
            candidates = [item for item in self.repository.list_equipment() if item["model_type"] == model_type]
            device = self.rules.select_device(candidates, now)
            if device is None:
                raise Conflict("暂无消毒有效且机型匹配的设备")
            try:
                return self._assign(request, device, int(expected_version), now, actor)
            except EquipmentUnavailable:
                continue
        raise Conflict("设备派单竞争激烈，请刷新后重试")

    def _assign(self, request: Dict[str, Any], equipment: Dict[str, Any], expected_version: int, now, actor: Actor) -> Dict[str, Any]:
        expected_return_at = format_instant(now + timedelta(hours=int(request["payload"]["estimated_hours"])))
        payload = dict(request["payload"])
        payload["dispatched_at"] = format_instant(now)
        payload["expected_return_at"] = expected_return_at
        return self.repository.assign_device(
            request_id=request["id"],
            request_version=expected_version,
            equipment_id=equipment["id"],
            equipment_version=equipment["version"],
            request_payload=payload,
            expected_return_at=expected_return_at,
            min_valid_until=format_instant(now),
            actor_id=actor.user_id,
        )

    def return_equipment(self, actor: Actor, code: str, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_action(actor, "return")
        equipment, request = self.repository.return_equipment(code, int(expected_version), actor.user_id)
        return {"equipment": equipment, "request": request}

    def confirm_disinfection(self, actor: Actor, code: str, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_action(actor, "confirm_disinfection")
        valid_until = self.rules.validate_disinfection(self._data(data), utc_now())
        return self.repository.confirm_disinfection(code, int(expected_version), valid_until, actor.user_id)

    def cancel_request(self, actor: Actor, request_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_action(actor, "cancel_request")
        reason = optional_text(self._data(data), "cancel_reason")
        return self.repository.cancel_request(request_id, int(expected_version), reason, actor.user_id)

    def equipment_timeline(self, actor: Actor, code: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        equipment = self.repository.get_equipment(code)
        return self.repository.timeline("equipment", equipment["id"])

    def request_timeline(self, actor: Actor, request_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        request = self.repository.get_request(request_id)
        return self.repository.timeline("request", request["id"])

    def stats(self, actor: Actor) -> Dict[str, Dict[str, int]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
