import tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def future(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


class ExtensionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "seepage defect", "description": "seepage at section 3",
             "severity": 'major', "quantity": 12, "threshold": 6,
             "external_ref": "EXT-1"}, "creator", 'inspector')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _close_item(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_initial_deadline_recorded(self):
        self.assertIn("deadline_at", self.item)
        self.assertGreater(self.item["deadline_at"], self.item["created_at"])
        self.assertIsNone(self.item["pending_extension"])

    def test_request_keeps_original_deadline_until_approved(self):
        new_deadline = future(96)
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": new_deadline, "reason": "汛期设备停用"},
            "engineer", 'dam_engineer')
        self.assertEqual(req["status"], "pending")
        during = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(during["deadline_at"], self.item["deadline_at"])
        self.assertEqual(during["pending_extension"]["id"], req["id"])
        decided = self.service.decide_extension(
            self.item["id"], req["id"], {"decision": "approve"}, "manager",
            'emergency_manager')
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["decided_by"], "manager")
        after = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(after["deadline_at"], new_deadline)
        self.assertEqual(after["version"], during["version"] + 1)
        self.assertIsNone(after["pending_extension"])
        events = self.service.audit("viewer", self.item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("extension_request", actions)
        self.assertIn("extension_decision", actions)
        decision_event = [e for e in events if e["action"] == "extension_decision"][0]
        self.assertEqual(decision_event["detail"]["decision"], "approved")
        self.assertEqual(decision_event["detail"]["deadline_before"],
                         self.item["deadline_at"])
        self.assertEqual(decision_event["detail"]["deadline_after"], new_deadline)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reject_keeps_original_deadline(self):
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "汛期设备停用"},
            "engineer", 'dam_engineer')
        decided = self.service.decide_extension(
            self.item["id"], req["id"], {"decision": "reject"}, "manager",
            'emergency_manager')
        self.assertEqual(decided["status"], "rejected")
        after = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(after["deadline_at"], self.item["deadline_at"])
        self.assertEqual(after["version"], self.item["version"])
        events = self.service.audit("viewer", self.item["id"])
        decision_event = [e for e in events if e["action"] == "extension_decision"][0]
        self.assertEqual(decision_event["detail"]["decision"], "rejected")
        self.assertEqual(decision_event["detail"]["deadline_after"],
                         self.item["deadline_at"])

    def test_single_pending_and_resubmit_after_decision(self):
        self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "第一次"},
            "engineer", 'dam_engineer')
        with self.assertRaises(ConflictError):
            self.service.request_extension(
                self.item["id"], {"new_deadline": future(120), "reason": "重复提交"},
                "engineer", 'dam_engineer')
        req = self.service.list_extensions(self.item["id"], "viewer")[0]
        self.service.decide_extension(
            self.item["id"], req["id"], {"decision": "reject"}, "manager",
            'emergency_manager')
        again = self.service.request_extension(
            self.item["id"], {"new_deadline": future(120), "reason": "驳回后重提"},
            "engineer", 'dam_engineer')
        self.assertEqual(again["status"], "pending")
        self.assertEqual(len(self.service.list_extensions(self.item["id"], "viewer")), 2)

    def test_requester_cannot_approve_own_request(self):
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "汛期设备停用"},
            "manager", 'dam_engineer')
        with self.assertRaises(PermissionDenied):
            self.service.decide_extension(
                self.item["id"], req["id"], {"decision": "approve"}, "manager",
                'emergency_manager')

    def test_second_decision_conflicts(self):
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "汛期设备停用"},
            "engineer", 'dam_engineer')
        self.service.decide_extension(
            self.item["id"], req["id"], {"decision": "approve"}, "manager-a",
            'emergency_manager')
        with self.assertRaises(ConflictError):
            self.service.decide_extension(
                self.item["id"], req["id"], {"decision": "reject"}, "manager-b",
                'emergency_manager')

    def test_concurrent_decision_only_one_succeeds(self):
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "汛期设备停用"},
            "engineer", 'dam_engineer')
        results, conflicts = [], []

        def decide(name):
            try:
                results.append(self.service.decide_extension(
                    self.item["id"], req["id"], {"decision": "approve"}, name,
                    'emergency_manager'))
            except ConflictError:
                conflicts.append(name)

        threads = [threading.Thread(target=decide, args=(f"manager-{i}",))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(conflicts), 1)
        after = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(after["deadline_at"], req["requested_deadline"])

    def test_closed_defect_not_accepted(self):
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "汛期设备停用"},
            "engineer", 'dam_engineer')
        closed = self._close_item()
        self.assertEqual(closed["status"], STATES[-1])
        with self.assertRaises(ConflictError):
            self.service.request_extension(
                closed["id"], {"new_deadline": future(120), "reason": "已关闭"},
                "engineer", 'dam_engineer')
        with self.assertRaises(ConflictError):
            self.service.decide_extension(
                closed["id"], req["id"], {"decision": "approve"}, "manager",
                'emergency_manager')

    def test_validation_and_roles(self):
        with self.assertRaises(PermissionDenied):
            self.service.request_extension(
                self.item["id"], {"new_deadline": future(96), "reason": "x"},
                "engineer", 'inspector')
        with self.assertRaises(ValidationError):
            self.service.request_extension(
                self.item["id"], {"new_deadline": "not-a-date", "reason": "x"},
                "engineer", 'dam_engineer')
        with self.assertRaises(ValidationError):
            self.service.request_extension(
                self.item["id"], {"new_deadline": future(96), "reason": " "},
                "engineer", 'dam_engineer')
        earlier = (datetime.fromisoformat(self.item["deadline_at"])
                   - timedelta(hours=1)).isoformat()
        with self.assertRaises(ValidationError):
            self.service.request_extension(
                self.item["id"], {"new_deadline": earlier, "reason": "提前不算延期"},
                "engineer", 'dam_engineer')
        req = self.service.request_extension(
            self.item["id"], {"new_deadline": future(96), "reason": "汛期设备停用"},
            "engineer", 'dam_engineer')
        with self.assertRaises(PermissionDenied):
            self.service.decide_extension(
                self.item["id"], req["id"], {"decision": "approve"}, "manager",
                'dam_engineer')
        with self.assertRaises(ValidationError):
            self.service.decide_extension(
                self.item["id"], req["id"], {"decision": "maybe"}, "manager",
                'emergency_manager')
        other = self.service.create_item(
            {"title": "crack defect", "description": "crack at section 5",
             "severity": 'minor', "quantity": 2, "threshold": 4,
             "external_ref": "EXT-2"}, "creator", 'inspector')
        with self.assertRaises(NotFoundError):
            self.service.decide_extension(
                other["id"], req["id"], {"decision": "approve"}, "manager",
                'emergency_manager')


if __name__ == "__main__":
    unittest.main()
