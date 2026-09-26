import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_equipment_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


KEEPER = Actor("keeper-1", "equipment_keeper")
DEPT_A = Actor("dept-a", "department_user")
DEPT_B = Actor("dept-b", "department_user")


def future(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def past(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class EquipmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_equipment_service(str(Path(self.temp.name) / "equip.db"))

    def tearDown(self):
        self.temp.cleanup()

    def register(self, code, model="turbine", valid_until=None):
        return self.service.register_device(KEEPER, {
            "code": code, "model": model,
            "disinfection_valid_until": valid_until or future(48),
        })

    def apply(self, actor, model="turbine", destination="抢救室"):
        return self.service.create_request(actor, {
            "requester": actor.user_id, "model": model,
            "duration_hours": 4, "destination": destination,
        })

    def test_register_and_dispatchable_list(self):
        self.register("T-201")
        self.register("T-202", valid_until=past(2))  # 超期
        board = self.service.board(KEEPER)
        codes = [d["code"] for d in board["dispatchable"]]
        self.assertEqual(codes, ["T-201"])
        self.assertEqual([d["code"] for d in board["expired"]], ["T-202"])

    def test_models_never_mixed(self):
        self.register("O-101", model="oxygen_driven")
        request = self.apply(DEPT_A, model="turbine")
        # 只有氧气驱动设备时，涡轮申请派不出单
        with self.assertRaises(Conflict):
            self.service.dispatch(KEEPER, request["id"])
        with self.assertRaises(ValidationError):
            self.service.dispatch(KEEPER, request["id"], device_code="O-101")

    def test_two_applicants_one_device_later_waits_with_code(self):
        self.register("T-201")
        first = self.apply(DEPT_A, destination="抢救室")
        second = self.apply(DEPT_B, destination="ICU")
        # 先提交的申请派单成功
        done = self.service.dispatch(KEEPER, first["id"])
        self.assertEqual(done["device_code"], "T-201")
        # 后提交的申请派不出单，留在待派区并显示等待设备编号
        with self.assertRaises(Conflict) as ctx:
            self.service.dispatch(KEEPER, second["id"])
        self.assertIn("T-201", str(ctx.exception))
        board = self.service.board(KEEPER)
        pending = [r for r in board["pending_area"] if r["id"] == second["id"]]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["waiting_for"], "T-201")

    def test_return_then_disinfect_before_redispatch(self):
        self.register("T-201")
        first = self.apply(DEPT_A)
        self.service.dispatch(KEEPER, first["id"])
        # 归还后转待消毒，不再可派
        device = self.service.return_device(DEPT_A, "T-201")
        self.assertEqual(device["state"], "pending_disinfection")
        board = self.service.board(KEEPER)
        self.assertEqual(board["dispatchable"], [])
        second = self.apply(DEPT_B)
        with self.assertRaises(Conflict):
            self.service.dispatch(KEEPER, second["id"])
        # 确认消毒后才能再次派单
        self.service.confirm_disinfection(KEEPER, "T-201", {"disinfection_valid_until": future(72)})
        done = self.service.dispatch(KEEPER, second["id"])
        self.assertEqual(done["device_code"], "T-201")

    def test_expired_device_needs_disinfection_confirm(self):
        self.register("T-209", valid_until=past(1))
        board = self.service.board(KEEPER)
        self.assertEqual(board["dispatchable"], [])
        device = self.service.confirm_disinfection(KEEPER, "T-209", {"disinfection_valid_until": future(24)})
        self.assertEqual(device["state"], "available")
        board = self.service.board(KEEPER)
        self.assertEqual([d["code"] for d in board["dispatchable"]], ["T-209"])

    def test_dispatched_device_cannot_be_dispatched_twice(self):
        self.register("T-201")
        first = self.apply(DEPT_A)
        second = self.apply(DEPT_B)
        self.service.dispatch(KEEPER, first["id"])
        with self.assertRaises(Conflict):
            self.service.dispatch(KEEPER, second["id"], device_code="T-201")

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_device(DEPT_A, {"code": "T-201", "model": "turbine", "disinfection_valid_until": future(1)})
        self.register("T-201")
        request = self.apply(DEPT_A)
        with self.assertRaises(PermissionDenied):
            self.service.dispatch(DEPT_A, request["id"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_disinfection(DEPT_A, "T-201", {"disinfection_valid_until": future(1)})

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.service.register_device(KEEPER, {"code": "", "model": "turbine", "disinfection_valid_until": future(1)})
        with self.assertRaises(ValidationError):
            self.service.create_request(DEPT_A, {"requester": "急诊", "model": "turbine", "duration_hours": 0, "destination": "抢救室"})
        with self.assertRaises(ValidationError):
            self.service.register_device(KEEPER, {"code": "T-202", "model": "turbine", "disinfection_valid_until": "not-a-time"})


if __name__ == "__main__":
    unittest.main()
