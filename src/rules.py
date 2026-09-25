from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")
    if data.get("parent_id"):
        _ensure_parent_chain(data, lookup)


def _ensure_parent_chain(data, lookup):
    """来源批次必须存在，且沿来源链向上不能成环。"""
    if lookup is None:
        raise ValidationError("parent_id cannot be verified without lookup")
    new_id = data.get("id")
    seen = set()
    current = data.get("parent_id")
    while current:
        if current in seen or current == new_id:
            raise ValidationError("consignment parent chain must not contain cycles")
        seen.add(current)
        parent = _find_one(lookup, "consignment", "id", current)
        if parent is None:
            raise ValidationError("parent consignment not found: " + str(current))
        current = (parent.get("data") or {}).get("parent_id")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_recheck(actor, entity, data, lookup):
    result = data.get("recheck_result")
    if result not in ("passed", "failed"):
        raise ValidationError("recheck_result must be 'passed' or 'failed'")
    if result != "passed":
        raise ValidationError("recheck not passed; consignment remains quarantined")
    return {"rechecked_by": actor.user_id}


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    _ensure_upstream_not_quarantined(entity, lookup)
    return {"released_by": actor.user_id}


def _ensure_upstream_not_quarantined(entity, lookup):
    """上游任一批次仍隔离时，下游不能放行。"""
    if lookup is None:
        return
    seen = set()
    current = (entity.get("data") or {}).get("parent_id")
    while current and current not in seen:
        seen.add(current)
        parent = _find_one(lookup, "consignment", "id", current)
        if parent is None:
            return
        if parent.get("status") == "quarantined":
            raise ValidationError(
                "upstream consignment %s is still quarantined" % current
            )
        current = (parent.get("data") or {}).get("parent_id")


def _validate_trace(actor, entity, data, lookup):
    """从起点批次给出完整传播路径、每批风险状态和受影响种植点。

    同一批重报时沿用该温室首次追溯记录的结果。
    """
    if lookup is None:
        raise ValidationError("trace requires consignment lookup")
    roots = []
    for start_id in data.get("consignment_ids") or []:
        if start_id not in roots:
            roots.append(start_id)
    previous = (entity.get("data") or {}).get("trace_report") or {}
    reused = {item.get("id"): item for item in previous.get("batches", [])}
    batches = {}
    paths = []
    destinations = []
    for start_id in roots:
        start = _find_one(lookup, "consignment", "id", start_id)
        if start is None:
            raise ValidationError("unknown consignment: " + str(start_id))
        order = _collect_downstream(lookup, start)
        links = [
            {"id": item.get("id"), "parent_id": (item.get("data") or {}).get("parent_id")}
            for item in order
        ]
        paths.extend(trace_paths(links, start_id))
        for item in order:
            item_id = item.get("id")
            if item_id in reused:
                batches[item_id] = reused[item_id]
            elif item_id not in batches:
                batches[item_id] = _batch_entry(item)
            destination = (item.get("data") or {}).get("destination")
            if destination and destination not in destinations:
                destinations.append(destination)
    facilities = []
    seen_facilities = set()
    for name in destinations:
        for facility in lookup("facility", "name", name) or []:
            facility_id = facility.get("id")
            if facility_id in seen_facilities:
                continue
            seen_facilities.add(facility_id)
            facilities.append({
                "id": facility_id,
                "name": (facility.get("data") or {}).get("name"),
                "status": facility.get("status"),
            })
    ordered = []
    for path in paths:
        for batch_id in path:
            if batch_id in batches and batch_id not in ordered:
                ordered.append(batch_id)
    report = {
        "traced_by": actor.user_id,
        "roots": roots,
        "paths": paths,
        "batches": [batches[batch_id] for batch_id in ordered],
        "facilities": facilities,
    }
    return {"trace_report": report}


def _collect_downstream(lookup, start):
    order = []
    seen = set()
    pending = [start]
    while pending:
        current = pending.pop(0)
        current_id = current.get("id")
        if current_id in seen:
            continue
        seen.add(current_id)
        order.append(current)
        children = lookup("consignment", "parent_id", current_id) or []
        pending.extend(sorted(children, key=lambda item: item.get("id")))
    return order


def _batch_entry(entity):
    data = entity.get("data") or {}
    return {
        "id": entity.get("id"),
        "code": data.get("code"),
        "status": entity.get("status"),
        "risk": consignment_risk(entity),
    }


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


def trace_paths(consignments, start_id):
    """返回从起点到每个末端批次的全部传播路径（按子批次id排序，确定顺序）。"""
    children = {}
    for item in consignments:
        children.setdefault(item.get("parent_id"), []).append(item.get("id"))
    for group in children.values():
        group.sort()
    paths = []
    stack = [(start_id, [start_id])]
    while stack:
        node, path = stack.pop()
        next_nodes = [child for child in children.get(node, []) if child not in path]
        if not next_nodes:
            paths.append(path)
            continue
        for child in reversed(next_nodes):
            stack.append((child, path + [child]))
    return paths


def consignment_risk(entity):
    """根据批次状态推导风险状态。"""
    status = entity.get("status")
    data = entity.get("data") or {}
    if status == "quarantined":
        return "isolated"
    if status == "destroyed":
        return "eliminated"
    if status == "released":
        return "cleared"
    if data.get("pest_found"):
        return "infected"
    if status == "inspected":
        return "observing"
    return "pending"


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'recheck'): _validate_recheck,
    ('consignment', 'release'): _validate_release,
    ('facility', 'trace'): _validate_trace,
}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected',), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered', 'traced'), 'traced')}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('pest_found', 'treatment'), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id', 'recheck_result'), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine')}

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
