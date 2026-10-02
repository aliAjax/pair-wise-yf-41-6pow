from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import (
    RuleEngine,
    plan_merge,
    plan_split,
    recompute_magnitude,
    validate_backfill,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if action == "merge" and entity["kind"] == "event":
            return self._merge_events(actor, entity, dict(data or {}), expected_version)
        if action == "split" and entity["kind"] == "event":
            return self._split_event(actor, entity, dict(data or {}), expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _merge_events(self, actor, target, data, expected_version):
        source_id = data.get("source_id")
        source = self.repository.get_entity(str(source_id)) if source_id else None
        plan = plan_merge(actor, target, source, data)
        target_status, target_data = plan["target"]
        source_status, source_data = plan["source"]
        expected = (
            int(expected_version) if expected_version is not None else target["version"]
        )
        try:
            self.repository.apply_changes(
                updates=[
                    (target["id"], expected, target_status, target_data),
                    (source["id"], source["version"], source_status, source_data),
                ]
            )
        except ConflictError as exc:
            raise self._explain_merge_conflict(exc, [target["id"], source["id"]])
        self.audit.record(
            target["id"], actor, "merge", target["status"], target_status, plan["detail"]
        )
        self.audit.record(
            source["id"],
            actor,
            "merge",
            source["status"],
            source_status,
            {"merged_into": target["id"]},
        )
        return self.repository.get_entity(target["id"])

    def _explain_merge_conflict(self, error, entity_ids):
        """并发合并同一对事件时先提交的生效；后提交的从这里看到并到了哪条。"""
        for entity_id in entity_ids:
            entity = self.repository.get_entity(entity_id)
            if entity and entity["status"] == "merged":
                return ConflictError(
                    "event %s already merged into %s"
                    % (entity_id, entity["data"].get("merged_into"))
                )
        return error

    def _split_event(self, actor, source, data, expected_version):
        target = None
        target_id = data.get("target_id")
        if target_id:
            target = self.repository.get_entity(str(target_id))
        destination_id = (
            str(target_id)
            if target_id
            else str((data.get("new_event") or {}).get("id") or uuid4())
        )
        if not target_id and self.repository.get_entity(destination_id):
            raise ConflictError("entity already exists: " + destination_id)
        plan = plan_split(actor, source, data, target, destination_id)
        source_status, source_data = plan["source"]
        expected = (
            int(expected_version) if expected_version is not None else source["version"]
        )
        updates = [(source["id"], expected, source_status, source_data)]
        creations = []
        if "new_event" in plan:
            new_status, new_data = plan["new_event"]
            creations.append(
                (destination_id, "event", new_status, new_data, actor.user_id)
            )
        if "target" in plan:
            target_status, target_data = plan["target"]
            updates.append((target["id"], target["version"], target_status, target_data))
        self.repository.apply_changes(creations=creations, updates=updates)
        self.audit.record(
            source["id"], actor, "split", source["status"], source_status, plan["detail"]
        )
        if creations:
            self.audit.record(
                destination_id,
                actor,
                "create",
                None,
                plan["new_event"][0],
                {"kind": "event", "split_from": source["id"]},
            )
        if "target" in plan:
            self.audit.record(
                target["id"],
                actor,
                "split_receive",
                target["status"],
                plan["target"][0],
                {"source_id": source["id"], "stations": plan["detail"]["stations"]},
            )
        return self.repository.get_entity(source["id"])

    def backfill_magnitudes(self, actor):
        """旧数据回填：事件缺震级且报文带震级时，按报文中位数回填。"""
        validate_backfill(actor)
        updated = []
        for event in self.repository.list_entities(kind="event"):
            data = event["data"]
            if data.get("magnitude") is not None:
                continue
            magnitude = recompute_magnitude(data.get("reports"))
            if magnitude is None:
                continue
            new_data = dict(data)
            new_data["magnitude"] = magnitude
            saved = self.repository.update_entity(
                event["id"], event["version"], event["status"], new_data
            )
            self.audit.record(
                event["id"],
                actor,
                "backfill_magnitude",
                event["status"],
                event["status"],
                {"magnitude": magnitude},
            )
            updated.append(saved)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
