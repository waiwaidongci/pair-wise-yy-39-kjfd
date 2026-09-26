from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, PermissionDenied,
                     ValidationError, ensure_role, normalize_severity,
                     require_number, require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EXTENSION_APPLY_ROLES,
                    EXTENSION_DECIDE_ROLES, RECORD_ROLES, TERMINAL_STATES,
                    TITLE, VIEW_ROLES, completion_blockers, effective_deadline,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def request_extension(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, EXTENSION_APPLY_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("缺陷已关闭，不再受理延期申请")
        reason = require_text(payload.get("reason"), "reason")
        new_deadline = require_timestamp(
            payload.get("requested_deadline"), "requested_deadline")
        if new_deadline <= datetime.now(timezone.utc):
            raise ValidationError("新的完成时间必须晚于当前时间")
        current_deadline = effective_deadline(item)
        if new_deadline <= datetime.fromisoformat(current_deadline):
            raise ValidationError("新的完成时间必须晚于当前期限")
        if self.repository.pending_extension(item_id) is not None:
            raise ConflictError("该缺陷已有待审批的延期申请")
        deadline_iso = new_deadline.isoformat()
        request = self.repository.create_extension(item_id, deadline_iso, reason, actor)
        self.repository.append_audit("extension_request", ENTITY, item_id, actor, {
            "request_id": request["id"], "requested_deadline": deadline_iso,
            "current_deadline": current_deadline, "reason": reason,
        })
        return request

    def decide_extension(self, item_id: int, request_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, EXTENSION_DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = payload.get("decision")
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision必须是approved或rejected")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note")
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("缺陷已关闭，延期申请不再受理")
        request = self.repository.get_extension(request_id)
        if request["item_id"] != item_id:
            raise NotFoundError("延期申请不存在")
        if request["requested_by"] == actor:
            raise PermissionDenied("申请人不能审批自己的延期申请")
        if request["status"] != "pending":
            raise ConflictError("该申请已被处理")
        current_deadline = effective_deadline(item)
        updated = self.repository.decide_extension(request_id, decision, actor, note)
        self.repository.append_audit("extension_decision", ENTITY, item_id, actor, {
            "request_id": request_id, "decision": decision,
            "requested_by": request["requested_by"],
            "previous_deadline": current_deadline,
            "new_deadline": (request["requested_deadline"]
                             if decision == "approved" else current_deadline),
            "note": note,
        })
        return updated

    def list_extensions(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_extensions(item_id)

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_at"] = effective_deadline(item)
        return result
