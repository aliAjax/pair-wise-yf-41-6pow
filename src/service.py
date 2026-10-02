from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    magnitude_from_reports,
    merge_reports,
    split_reports,
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
        if kind == "event":
            self._assign_report_ids(payload)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    @staticmethod
    def _assign_report_ids(payload):
        for report in payload.get("reports") or []:
            if not report.get("id"):
                report["id"] = uuid4().hex

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "event" and action == "merge":
            return self._merge(actor, entity, data or {}, expected_version)
        if entity["kind"] == "event" and action == "split":
            return self._split(actor, entity, data or {}, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged, action=action)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _merge(self, actor, target, data, expected_version):
        source_id = data.get("source_event_id") or data.get("other") or data.get("source")
        if not source_id:
            raise ValidationError("source_event_id is required for merge")
        source = self.repository.get_entity(source_id)
        if not source:
            raise NotFoundError("entity not found: " + source_id)
        self.rules.validate_merge(actor, target, source)
        target_reports = target["data"].get("reports") or []
        source_reports = source["data"].get("reports") or []
        combined = merge_reports(target_reports, source_reports)
        for report in combined:
            report.setdefault("id", uuid4().hex)
        magnitude = magnitude_from_reports(combined)
        merged_target, merged_source = self.repository.merge_events(
            target["id"],
            source_id,
            combined,
            magnitude,
            actor.user_id,
            expected_version,
        )
        self.audit.record(
            target["id"], actor, "merge", target["status"], merged_target["status"],
            {"source": source_id, "combined_count": len(combined)},
        )
        self.audit.record(
            source_id, actor, "merge", source["status"], merged_source["status"],
            {"merged_into": target["id"]},
        )
        return merged_target

    def _split(self, actor, event, data, expected_version):
        report_ids = data.get("report_ids") or []
        target_id = data.get("target_event_id")
        new_event_data = None
        if not target_id:
            new_event_data = {
                "title": data.get("title"),
                "origin_time": data.get("origin_time"),
                "location": data.get("location"),
            }
            if not new_event_data["title"]:
                raise ValidationError("title is required to split into a new event")
        self.rules.validate_split(actor, event, report_ids)
        keep, moved = split_reports(event["data"].get("reports") or [], report_ids)
        if not moved:
            raise ValidationError("no reports matched report_ids")
        for report in moved:
            report.setdefault("id", uuid4().hex)
        magnitude = magnitude_from_reports(keep)
        target_before = None
        if target_id:
            target_before = self.repository.get_entity(target_id)
            if not target_before:
                raise NotFoundError("entity not found: " + target_id)
        updated, target = self.repository.split_event(
            event["id"],
            keep,
            moved,
            target_id,
            new_event_data,
            magnitude,
            actor.user_id,
            expected_version,
        )
        self.audit.record(
            event["id"], actor, "split", event["status"], updated["status"],
            {"moved": [r.get("id") for r in moved], "target": target["id"]},
        )
        if target_id:
            self.audit.record(
                target["id"], actor, "split", target_before["status"], target["status"],
                {"received": [r.get("id") for r in moved]},
            )
        return updated

    def backfill_magnitudes(self, actor):
        """Recompute magnitude for every event from its station reports."""
        self.rules._ensure_role(actor, ("admin", "analyst"))
        updated = []
        for event in self.repository.list_entities(kind="event"):
            if event["status"] == "merged" or event["data"].get("merged_into"):
                continue
            reports = event["data"].get("reports") or []
            magnitude = magnitude_from_reports(reports)
            if magnitude is None:
                continue
            if event["data"].get("magnitude") == magnitude:
                continue
            data = dict(event["data"])
            data["magnitude"] = magnitude
            entity = self.repository.update_entity(
                event["id"], event["version"], event["status"], data, action="backfill"
            )
            self.audit.record(
                event["id"], actor, "backfill", event["status"], entity["status"],
                {"magnitude": magnitude},
            )
            updated.append(entity)
        return {"updated": len(updated), "items": updated}

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

    def list_versions(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_versions(entity_id)

    def get_version(self, entity_id, version):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        record = self.repository.get_version(entity_id, version)
        if not record:
            raise NotFoundError("version not found: " + str(version))
        return record
