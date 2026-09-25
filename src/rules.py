from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_consignment(actor, data, lookup, entity_id=None):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")
    source_ids = data.get("source_ids") or []
    if not isinstance(source_ids, list) or any(
        not isinstance(item, str) or not item for item in source_ids
    ):
        raise ValidationError("source_ids must be a list of consignment ids")
    if len(set(source_ids)) != len(source_ids):
        raise ValidationError("source_ids must not contain duplicates")
    if lookup is None:
        return
    for source_id in source_ids:
        if _find_one(lookup, "consignment", "id", source_id) is None:
            raise ValidationError("unknown source consignment: " + source_id)
    if entity_id:
        _assert_acyclic(entity_id, source_ids, lookup)


def _assert_acyclic(entity_id, source_ids, lookup):
    if entity_id in source_ids:
        raise ValidationError("source relationship would form a cycle")
    seen = set()
    stack = list(source_ids)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == entity_id:
            raise ValidationError("source relationship would form a cycle")
        source = _find_one(lookup, "consignment", "id", current)
        if source:
            stack.extend(source["data"].get("source_ids") or [])


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _validate_recheck(actor, entity, data, lookup):
    result = data.get("recheck_result")
    if result not in ("pass", "fail"):
        raise ValidationError("recheck_result must be pass or fail")
    if result != "pass":
        raise ValidationError("recheck not passed; consignment remains quarantined")
    return {
        "pest_found": False,
        "rechecked_by": actor.user_id,
        "recheck_conclusion": data.get("conclusion") or "recheck pass",
    }


def _validate_release(actor, entity, data, lookup):
    if data.get("pest_found"):
        raise ValidationError("pest-positive consignment cannot be released")
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    if lookup is not None:
        blocked = _quarantined_upstream(entity, lookup)
        if blocked:
            raise ValidationError("upstream consignment still quarantined: " + blocked)
    return {"released_by": actor.user_id}


def _quarantined_upstream(entity, lookup):
    seen = set()
    stack = list(entity["data"].get("source_ids") or [])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        source = _find_one(lookup, "consignment", "id", current)
        if not source:
            continue
        if source["status"] == "quarantined":
            return current
        stack.extend(source["data"].get("source_ids") or [])
    return None


def _validate_trace(actor, entity, data, lookup):
    start_ids = list(dict.fromkeys(data.get("consignment_ids") or []))
    consignments = lookup("consignment", None, None) if lookup else []
    by_id = {item["id"]: item for item in consignments}
    for start_id in start_ids:
        if start_id not in by_id:
            raise ValidationError("unknown consignment: " + str(start_id))
    paths, involved = trace_propagation(consignments, start_ids)
    affected_names = set()
    for batch_id in involved:
        info = by_id[batch_id]["data"]
        affected_names.add(info.get("origin"))
        affected_names.add(info.get("destination"))
    facilities = lookup("facility", None, None) if lookup else []
    affected_facilities = sorted(
        (
            {"id": item["id"], "name": item["data"].get("name")}
            for item in facilities
            if item["data"].get("name") in affected_names
        ),
        key=lambda item: item["name"] or "",
    )
    batches = {}
    for batch_id in involved:
        item = by_id[batch_id]
        batches[batch_id] = {
            "code": item["data"].get("code"),
            "status": item["status"],
            "risk": risk_status(item),
        }
    return {
        "trace_result": {
            "starts": start_ids,
            "paths": paths,
            "batches": batches,
            "affected_facilities": affected_facilities,
            "traced_by": actor.user_id,
        }
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


def trace_propagation(consignments, start_ids):
    children = {}
    for item in consignments:
        for source in item["data"].get("source_ids") or []:
            children.setdefault(source, []).append(item["id"])
    for ids in children.values():
        ids.sort()
    paths = []

    def walk(node, path):
        next_ids = [child for child in children.get(node, []) if child not in path]
        if not next_ids:
            paths.append(path)
            return
        for child in next_ids:
            walk(child, path + [child])

    for start in start_ids:
        walk(start, [start])
    involved = sorted({batch_id for path in paths for batch_id in path})
    return paths, involved


def risk_status(entity):
    status = entity["status"]
    if status in ("quarantined", "destroyed", "released"):
        return status
    if entity["data"].get("pest_found"):
        return "suspect"
    if status == "inspected":
        return "inspected"
    return "declared"


CUSTOM_CREATE = {'consignment': _validate_consignment}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('consignment', 'recheck'): _validate_recheck, ('facility', 'trace'): _validate_trace}


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

    def validate_create(self, actor, kind, data, lookup=None, entity_id=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup, entity_id)
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
