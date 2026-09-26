import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_equipment_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


MANAGER = Actor("keeper-01", "equipment_manager")
REQUESTER = Actor("er-doctor", "department_requester")


def iso_in(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


class EquipmentServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_equipment_service(str(Path(self.temp.name) / "equipment.db"))

    def tearDown(self):
        self.temp.cleanup()

    def register(self, code="VENT-001", model="turbine", hours=8):
        return self.service.register_equipment(MANAGER, {"code": code, "model_type": model, "disinfection_valid_until": iso_in(hours=hours)})

    def apply(self, reference="REQ-1", model="turbine", desired="", hours=4):
        data = {"requester": "急诊科", "destination": "急诊抢救室", "model_type": model, "estimated_hours": hours}
        if desired:
            data["desired_equipment_code"] = desired
        return self.service.create_request(REQUESTER, reference, data)

    def test_full_turnover_cycle(self):
        equipment = self.register()
        self.assertEqual(equipment["state"], "available")
        request = self.apply(desired="VENT-001")
        dispatched = self.service.dispatch(MANAGER, request["id"], request["version"], {})
        self.assertEqual(dispatched["state"], "dispatched")
        self.assertEqual(dispatched["equipment_code"], "VENT-001")
        self.assertIn("expected_return_at", dispatched["payload"])
        equipment = self.service.get_equipment(MANAGER, "VENT-001")
        self.assertEqual(equipment["state"], "dispatched")
        self.assertFalse(equipment["dispatchable"])
        self.assertEqual(self.service.list_equipment(MANAGER, dispatchable_only=True), [])
        outcome = self.service.return_equipment(MANAGER, "VENT-001", equipment["version"])
        self.assertEqual(outcome["equipment"]["state"], "pending_disinfection")
        self.assertEqual(outcome["request"]["state"], "fulfilled")
        # 待消毒设备不能派出，确认后才能再次派单
        follow_up = self.apply(reference="REQ-2", desired="VENT-001")
        with self.assertRaises(Conflict):
            self.service.dispatch(MANAGER, follow_up["id"], follow_up["version"], {})
        confirmed = self.service.confirm_disinfection(MANAGER, "VENT-001", outcome["equipment"]["version"], {"disinfection_valid_until": iso_in(hours=12)})
        self.assertEqual(confirmed["state"], "available")
        dispatched = self.service.dispatch(MANAGER, follow_up["id"], follow_up["version"], {})
        self.assertEqual(dispatched["state"], "dispatched")
        timeline = self.service.equipment_timeline(MANAGER, "VENT-001")
        self.assertEqual([event["action"] for event in timeline], ["registered", "dispatched", "returned", "disinfection_confirmed", "dispatched"])

    def test_contention_second_request_waits_with_code(self):
        self.register()
        first = self.apply("REQ-A", desired="VENT-001")
        second = self.apply("REQ-B", desired="VENT-001")
        self.service.dispatch(MANAGER, first["id"], first["version"], {})
        result = self.service.dispatch(MANAGER, second["id"], second["version"], {})
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["waiting_equipment_code"], "VENT-001")
        pending = self.service.list_requests(MANAGER, state="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["reference"], "REQ-B")
        self.assertEqual(pending[0]["waiting_equipment_code"], "VENT-001")

    def test_model_mismatch_rejected(self):
        self.register("VENT-O2", model="oxygen_driven")
        request = self.apply(model="turbine", desired="VENT-O2")
        with self.assertRaises(ValidationError):
            self.service.dispatch(MANAGER, request["id"], request["version"], {"equipment_code": "VENT-O2"})

    def test_expired_device_excluded_from_dispatchable(self):
        # 直接写入一台已超期设备，模拟消毒有效期已过
        self.service.repository.create_equipment("VENT-OLD", "turbine", iso_in(hours=-1), "seed")
        equipment = self.service.get_equipment(MANAGER, "VENT-OLD")
        self.assertTrue(equipment["expired"])
        self.assertFalse(equipment["dispatchable"])
        codes = [item["code"] for item in self.service.list_equipment(MANAGER, dispatchable_only=True)]
        self.assertNotIn("VENT-OLD", codes)
        request = self.apply(desired="VENT-OLD")
        with self.assertRaises(Conflict):
            self.service.dispatch(MANAGER, request["id"], request["version"], {})

    def test_auto_dispatch_selects_matching_model(self):
        self.register("VENT-O2", model="oxygen_driven")
        self.register("VENT-T1", model="turbine")
        request = self.apply(model="turbine")
        result = self.service.dispatch(MANAGER, request["id"], request["version"], {})
        self.assertEqual(result["equipment_code"], "VENT-T1")

    def test_no_matching_device_raises(self):
        self.register("VENT-O2", model="oxygen_driven")
        request = self.apply(model="turbine")
        with self.assertRaises(Conflict):
            self.service.dispatch(MANAGER, request["id"], request["version"], {})

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_equipment(Actor("outsider", "outsider"), {"code": "VENT-1", "model_type": "turbine", "disinfection_valid_until": iso_in(hours=8)})
        with self.assertRaises(PermissionDenied):
            self.service.register_equipment(REQUESTER, {"code": "VENT-1", "model_type": "turbine", "disinfection_valid_until": iso_in(hours=8)})
        with self.assertRaises(PermissionDenied):
            self.service.dispatch(REQUESTER, 1, 1, {})

    def test_version_conflict_and_duplicate_code(self):
        self.register()
        with self.assertRaises(Conflict):
            self.register()
        request = self.apply(desired="VENT-001")
        with self.assertRaises(Conflict):
            self.service.dispatch(MANAGER, request["id"], request["version"] + 1, {})

    def test_cancel_only_pending(self):
        self.register()
        request = self.apply(desired="VENT-001")
        dispatched = self.service.dispatch(MANAGER, request["id"], request["version"], {})
        with self.assertRaises(Conflict):
            self.service.cancel_request(REQUESTER, dispatched["id"], dispatched["version"], {})
        waiting = self.apply(reference="REQ-9")
        cancelled = self.service.cancel_request(REQUESTER, waiting["id"], waiting["version"], {"cancel_reason": "不再需要"})
        self.assertEqual(cancelled["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
