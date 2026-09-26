import tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ExtensionTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"seepage defect","description":"extension scenarios","severity":'major',"quantity":5,"threshold":10,"external_ref":"EXT-1"},"creator",'inspector')
        self.future=(datetime.now(timezone.utc)+timedelta(days=30)).replace(microsecond=0).isoformat()
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _apply(self,actor="engineer1",**over):
        payload={"requested_deadline":self.future,"reason":"汛期设备停用，无法进场施工"}; payload.update(over)
        return self.service.request_extension(self.item["id"],payload,actor,'dam_engineer')
    def _close_item(self):
        current=self.service.get_item(self.item["id"],"viewer")
        for target in STATES[1:]: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        return current
    def test_pending_keeps_original_deadline_and_approve_switches(self):
        original=self.service.get_item(self.item["id"],"viewer")["deadline_at"]
        request=self._apply()
        self.assertEqual(request["status"],"pending")
        during=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(during["deadline_at"],original)
        decided=self.service.decide_extension(self.item["id"],request["id"],{"decision":"approved","note":"同意延期"},"manager1",'emergency_manager')
        self.assertEqual(decided["status"],"approved"); self.assertEqual(decided["decided_by"],"manager1")
        after=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(after["deadline_at"],self.future); self.assertNotEqual(after["deadline_at"],original)
        events=self.service.audit("viewer",self.item["id"])
        actions=[e["action"] for e in events]
        self.assertIn("extension_request",actions); self.assertIn("extension_decision",actions)
        decision_event=[e for e in events if e["action"]=="extension_decision"][0]
        self.assertEqual(decision_event["detail"]["decision"],"approved")
        self.assertEqual(decision_event["detail"]["previous_deadline"],original)
        self.assertEqual(decision_event["detail"]["new_deadline"],self.future)
        self.assertTrue(self.repo.verify_audit_chain())
    def test_reject_keeps_original_deadline(self):
        original=self.service.get_item(self.item["id"],"viewer")["deadline_at"]
        request=self._apply()
        decided=self.service.decide_extension(self.item["id"],request["id"],{"decision":"rejected","note":"理由不充分"},"manager1",'emergency_manager')
        self.assertEqual(decided["status"],"rejected")
        self.assertEqual(self.service.get_item(self.item["id"],"viewer")["deadline_at"],original)
        again=self._apply()
        self.assertEqual(again["status"],"pending")
    def test_duplicate_pending_application_rejected(self):
        self._apply()
        with self.assertRaises(ConflictError) as ctx: self._apply(actor="engineer2")
        self.assertIn("已有待审批",str(ctx.exception))
        self.assertEqual(len(self.service.list_extensions(self.item["id"],"viewer")),1)
    def test_applicant_cannot_decide_own_request(self):
        request=self._apply(actor="zhangsan")
        with self.assertRaises(PermissionDenied): self.service.decide_extension(self.item["id"],request["id"],{"decision":"approved"},"zhangsan",'emergency_manager')
        self.assertEqual(self.service.list_extensions(self.item["id"],"viewer")[0]["status"],"pending")
    def test_concurrent_decide_only_one_succeeds(self):
        request=self._apply()
        results=[]
        def decide(name,decision):
            try:
                self.service.decide_extension(self.item["id"],request["id"],{"decision":decision},name,'emergency_manager'); results.append(("ok",name))
            except ConflictError: results.append(("conflict",name))
        threads=[threading.Thread(target=decide,args=("manager1","approved")),threading.Thread(target=decide,args=("manager2","rejected"))]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(r[0] for r in results),["conflict","ok"])
        final=self.service.list_extensions(self.item["id"],"viewer")[0]
        self.assertIn(final["status"],("approved","rejected"))
        with self.assertRaises(ConflictError): self.service.decide_extension(self.item["id"],request["id"],{"decision":"approved"},"manager3",'emergency_manager')
    def test_concurrent_apply_only_one_succeeds(self):
        results=[]
        def apply(name):
            try: self._apply(actor=name); results.append("ok")
            except ConflictError: results.append("conflict")
        threads=[threading.Thread(target=apply,args=(f"engineer{i}",)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results),["conflict","conflict","conflict","ok"])
        self.assertEqual(len(self.service.list_extensions(self.item["id"],"viewer")),1)
    def test_closed_item_rejects_apply_and_decide(self):
        request=self._apply()
        self._close_item()
        with self.assertRaises(ConflictError): self._apply()
        with self.assertRaises(ConflictError): self.service.decide_extension(self.item["id"],request["id"],{"decision":"approved"},"manager1",'emergency_manager')
    def test_role_guards(self):
        with self.assertRaises(PermissionDenied): self.service.request_extension(self.item["id"],{"requested_deadline":self.future,"reason":"x"},"a",'inspector')
        with self.assertRaises(PermissionDenied): self.service.request_extension(self.item["id"],{"requested_deadline":self.future,"reason":"x"},"a",'viewer')
        request=self._apply()
        with self.assertRaises(PermissionDenied): self.service.decide_extension(self.item["id"],request["id"],{"decision":"approved"},"b",'dam_engineer')
    def test_validation(self):
        with self.assertRaises(ValidationError): self._apply(reason="  ")
        with self.assertRaises(ValidationError): self._apply(requested_deadline="not-a-date")
        past=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        with self.assertRaises(ValidationError): self._apply(requested_deadline=past)
        sooner=(datetime.fromisoformat(self.item["created_at"])+timedelta(hours=1)).isoformat()
        with self.assertRaises(ValidationError): self._apply(requested_deadline=sooner)
        request=self._apply()
        with self.assertRaises(ValidationError): self.service.decide_extension(self.item["id"],request["id"],{"decision":"maybe"},"manager1",'emergency_manager')
if __name__=="__main__": unittest.main()
