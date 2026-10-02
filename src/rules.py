from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def dedupe_by_station(reports):
    """Keep a single report per station, preserving first occurrence order."""
    seen = set()
    result = []
    for report in reports:
        key = report.get("station")
        if key in seen:
            continue
        seen.add(key)
        result.append(report)
    return result


def merge_reports(target_reports, source_reports):
    """Union two report sets and drop duplicate station reports."""
    return dedupe_by_station(list(target_reports or []) + list(source_reports or []))


def report_amplitudes(reports):
    values = []
    for report in reports or []:
        amplitude = report.get("amplitude")
        if amplitude is not None and amplitude != "":
            values.append(float(amplitude))
    return values


def magnitude_from_reports(reports):
    """Recompute magnitude as the median of report amplitudes.

    Returns None when no amplitude is available so callers can leave the
    existing magnitude untouched.
    """
    values = report_amplitudes(reports)
    if not values:
        return None
    return round(magnitude_median(values), 2)


def split_reports(reports, report_ids):
    """Partition reports into (kept, moved) by report id."""
    selected = {str(value) for value in (report_ids or [])}
    moved = []
    keep = []
    for report in reports or []:
        if str(report.get("id")) in selected:
            moved.append(report)
        else:
            keep.append(report)
    return keep, moved


def post_event_status(current):
    """Reports changed after publish: the event must go back to pending review."""
    if current in ("published", "revised"):
        return "associated"
    return current


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer'), 'merge': ('admin', 'analyst'), 'split': ('admin', 'analyst')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def validate_merge(self, actor, target, source):
        """Validate that two events can be merged into one."""
        self._ensure_role(actor, self.ROLE_ACTIONS.get(("event", "merge"), ("admin", "analyst")))
        if target["kind"] != "event" or source["kind"] != "event":
            raise ValidationError("merge requires two events")
        if target["id"] == source["id"]:
            raise ValidationError("cannot merge an event with itself")
        for label, event in (("target", target), ("source", source)):
            merged_into = (event.get("data") or {}).get("merged_into")
            if event["status"] == "merged" or merged_into:
                raise ConflictError(
                    "%s event %s already merged into %s"
                    % (label, event["id"], merged_into)
                )
        return True

    def validate_split(self, actor, event, report_ids):
        """Validate that selected reports can be split out of an event."""
        self._ensure_role(actor, self.ROLE_ACTIONS.get(("event", "split"), ("admin", "analyst")))
        if event["kind"] != "event":
            raise ValidationError("split requires an event")
        if event["status"] == "merged" or (event.get("data") or {}).get("merged_into"):
            raise ConflictError(
                "event %s already merged into %s"
                % (event["id"], (event.get("data") or {}).get("merged_into"))
            )
        if not report_ids:
            raise ValidationError("report_ids are required for split")
        return True


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
