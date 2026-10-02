from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
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


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

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


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


# ---------------------------------------------------------------------------
# 事件合并与拆分
#
# 合并和拆分不走通用状态机（结果状态是算出来的），由 service 层调用这里的
# plan_* 函数完成校验和状态计算，再在单个事务里落库。
# ---------------------------------------------------------------------------

# 可以参与合并/拆分的状态；已撤回或已并入他条的事件不能再参与。
EVENT_ACTIVE_STATUSES = ("candidate", "associated", "reviewed", "published", "revised")
# 发布过的状态：合并或拆分后幸存事件退回待复核（associated），旧版外发内容留档。
EVENT_PUBLISHED_STATUSES = ("published", "revised")
MERGE_ROLES = ("admin", "analyst")
SPLIT_ROLES = ("admin", "analyst")
BACKFILL_ROLES = ("admin",)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_reports(primary, secondary):
    """合并两组报文：同台站重复报文只留一份（保留主事件原有报文）。"""
    combined = list(primary or [])
    seen = {
        report.get("station")
        for report in combined
        if report.get("station") is not None
    }
    for report in secondary or []:
        station = report.get("station")
        if station is not None and station in seen:
            continue
        combined.append(report)
        if station is not None:
            seen.add(station)
    return combined


def recompute_magnitude(reports):
    """按报文中的震级取中位数；报文没有可用震级时返回None表示不重算。"""
    values = []
    for report in reports or []:
        value = report.get("magnitude")
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    # 偶数个取均值会引入浮点尾数（如4.300000000000001），保留两位小数去掉噪声
    return round(magnitude_median(values), 2)


def publication_snapshot(entity, now):
    """已外发内容留作旧版，追加到previous_publications。"""
    data = entity["data"]
    return {
        "status": entity["status"],
        "version": entity["version"],
        "magnitude": data.get("magnitude"),
        "communication_id": data.get("communication_id"),
        "reviewer": data.get("reviewer"),
        "reports": list(data.get("reports") or []),
        "superseded_at": now,
    }


def _ensure_active(entity, action):
    if entity["status"] == "merged":
        raise ConflictError(
            "event %s already merged into %s"
            % (entity["id"], entity["data"].get("merged_into"))
        )
    if entity["status"] not in EVENT_ACTIVE_STATUSES:
        raise InvalidTransition(
            "cannot %s event in status %s" % (action, entity["status"])
        )


def plan_merge(actor, target, source, data, now=None):
    """校验并计算合并：source并入target，返回两边的新状态和审计明细。"""
    if actor.role not in MERGE_ROLES:
        raise PermissionDenied("role %s is not allowed here" % actor.role)
    source_id = data.get("source_id")
    if not source_id:
        raise ValidationError("missing required field: source_id")
    if source is None:
        raise NotFoundError("entity not found: " + str(source_id))
    if target["kind"] != "event" or source["kind"] != "event":
        raise ValidationError("merge only applies to events")
    if source["id"] == target["id"]:
        raise ValidationError("cannot merge an event into itself")
    _ensure_active(target, "merge")
    _ensure_active(source, "merge")
    now = now or _utcnow()

    target_data = dict(target["data"])
    source_data = dict(source["data"])
    target_reports = target_data.get("reports") or []
    source_reports = source_data.get("reports") or []
    combined = merge_reports(target_reports, source_reports)
    detail = {
        "source_id": source["id"],
        "reports_moved": len(source_reports),
        "reports_deduplicated": len(target_reports) + len(source_reports) - len(combined),
    }
    if data.get("reason"):
        detail["reason"] = data["reason"]
    target_data["reports"] = combined
    magnitude = recompute_magnitude(combined)
    if magnitude is not None:
        if magnitude != target_data.get("magnitude"):
            detail["magnitude_from"] = target_data.get("magnitude")
            detail["magnitude"] = magnitude
        target_data["magnitude"] = magnitude

    target_status = target["status"]
    if target["status"] in EVENT_PUBLISHED_STATUSES:
        target_data.setdefault("previous_publications", []).append(
            publication_snapshot(target, now)
        )
    if source["status"] in EVENT_PUBLISHED_STATUSES:
        source_data.setdefault("previous_publications", []).append(
            publication_snapshot(source, now)
        )
    if (
        target["status"] in EVENT_PUBLISHED_STATUSES
        or source["status"] in EVENT_PUBLISHED_STATUSES
    ):
        # 发布过的一边退回待复核
        target_status = "associated"

    # 一份报文只归一个事件：报文全部并入target，source不再持有报文
    source_data["reports"] = []
    source_data["merged_into"] = target["id"]
    source_data["merged_at"] = now
    if data.get("reason"):
        source_data["merge_reason"] = data["reason"]

    return {
        "target": (target_status, target_data),
        "source": ("merged", source_data),
        "detail": detail,
    }


def plan_split(actor, source, data, target, destination_id, now=None):
    """校验并计算拆分：选中的报文从source转到target（既有事件或新事件）。"""
    if actor.role not in SPLIT_ROLES:
        raise PermissionDenied("role %s is not allowed here" % actor.role)
    _ensure_active(source, "split")
    stations = data.get("stations")
    if (
        not stations
        or not isinstance(stations, list)
        or any(not station for station in stations)
    ):
        raise ValidationError("missing required field: stations")
    target_id = data.get("target_id")
    new_event = data.get("new_event")
    if target_id and new_event:
        raise ValidationError("provide either target_id or new_event, not both")
    if target_id:
        if target is None:
            raise NotFoundError("entity not found: " + str(target_id))
        if target["kind"] != "event":
            raise ValidationError("split target must be an event")
        if target["id"] == source["id"]:
            raise ValidationError("cannot split an event into itself")
        _ensure_active(target, "split")
    now = now or _utcnow()

    reports = source["data"].get("reports") or []
    known = {report.get("station") for report in reports}
    missing = [station for station in stations if station not in known]
    if missing:
        raise ValidationError(
            "reports not found for stations: "
            + ", ".join(str(station) for station in missing)
        )
    moved = [report for report in reports if report.get("station") in stations]
    remaining = [report for report in reports if report.get("station") not in stations]
    if len(remaining) < 2:
        raise ValidationError(
            "split would leave fewer than two reports on the source event"
        )
    if not target_id and len(moved) < 2:
        raise ValidationError("a new event requires at least two reports")
    if not target_id and not (new_event or {}).get("title"):
        raise ValidationError("missing required field: new_event.title")

    source_data = dict(source["data"])
    source_data["reports"] = remaining
    detail = {"stations": list(stations), "destination_id": destination_id}
    if data.get("reason"):
        detail["reason"] = data["reason"]
    magnitude = recompute_magnitude(remaining)
    if magnitude is not None:
        if magnitude != source_data.get("magnitude"):
            detail["magnitude_from"] = source_data.get("magnitude")
            detail["magnitude"] = magnitude
        source_data["magnitude"] = magnitude
    source_status = source["status"]
    if source["status"] in EVENT_PUBLISHED_STATUSES:
        source_data.setdefault("previous_publications", []).append(
            publication_snapshot(source, now)
        )
        source_status = "associated"
    source_data.setdefault("split_history", []).append(
        {"destination_id": destination_id, "stations": list(stations), "at": now}
    )

    plan = {"source": (source_status, source_data), "detail": detail}
    if target_id:
        target_data = dict(target["data"])
        combined = merge_reports(target_data.get("reports"), moved)
        target_data["reports"] = combined
        magnitude = recompute_magnitude(combined)
        if magnitude is not None:
            target_data["magnitude"] = magnitude
        target_status = target["status"]
        if target["status"] in EVENT_PUBLISHED_STATUSES:
            target_data.setdefault("previous_publications", []).append(
                publication_snapshot(target, now)
            )
            target_status = "associated"
        plan["target"] = (target_status, target_data)
    else:
        new_data = dict(new_event or {})
        new_data.pop("id", None)  # id已用作新事件实体id，不进data
        new_data.setdefault("origin_time", source["data"].get("origin_time"))
        new_data.setdefault("location", source["data"].get("location"))
        new_data["reports"] = moved
        magnitude = recompute_magnitude(moved)
        if magnitude is not None:
            new_data["magnitude"] = magnitude
        new_data["split_from"] = source["id"]
        plan["new_event"] = ("candidate", new_data)
    return plan


def validate_backfill(actor):
    if actor.role not in BACKFILL_ROLES:
        raise PermissionDenied("role %s is not allowed here" % actor.role)
