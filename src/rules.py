from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


FOUNDER_OVERLAP_LIMIT = 0.25


def founder_contribution_map(entity, lookup, memo=None, visiting=None):
    """Return {founder_id: fraction} of founder blood carried by one animal.

    Animals without a recorded sire are founders. Each recorded parent
    contributes half; a missing parent contributes nothing. Cycles in bad
    pedigree data are broken by returning an empty map.
    """
    if memo is None:
        memo = {}
    if visiting is None:
        visiting = set()
    animal_id = entity.get("id")
    if animal_id in memo:
        return memo[animal_id]
    data = entity.get("data", {})
    sire_id = data.get("sire_id")
    if not sire_id:
        result = {animal_id: 1.0}
    elif animal_id in visiting:
        result = {}
    else:
        visiting.add(animal_id)
        result = {}
        for parent_id in (sire_id, data.get("dam_id")):
            if not parent_id:
                continue
            parent = _find_one(lookup, "animal", "id", parent_id)
            if not parent:
                continue
            for founder_id, frac in founder_contribution_map(
                parent, lookup, memo, visiting
            ).items():
                result[founder_id] = result.get(founder_id, 0.0) + 0.5 * frac
        visiting.discard(animal_id)
    memo[animal_id] = result
    return result


def founder_overlap(sire, dam, lookup, memo=None):
    """Return (shared founder blood fraction, (top founder_id, amount))."""
    if memo is None:
        memo = {}
    sire_map = founder_contribution_map(sire, lookup, memo)
    dam_map = founder_contribution_map(dam, lookup, memo)
    shared = {}
    for founder_id, sire_frac in sire_map.items():
        dam_frac = dam_map.get(founder_id)
        if dam_frac:
            shared[founder_id] = min(sire_frac, dam_frac)
    top = max(shared.items(), key=lambda item: item[1]) if shared else None
    return sum(shared.values()), top


def founder_ledger(animals):
    """Founder blood shares among living animals, plus all carriers.

    Living means not deceased: quarantined animals still count in the pool.
    Deceased and quarantined animals remain listed as founders/carriers.
    """
    by_id = {animal["id"]: animal for animal in animals}

    def lookup(kind, field, value):
        if kind == "animal" and field == "id":
            entity = by_id.get(value)
            return [entity] if entity else []
        return []

    memo = {}
    contribs = {
        animal["id"]: founder_contribution_map(animal, lookup, memo)
        for animal in animals
    }
    living = [animal for animal in animals if animal["status"] != "deceased"]
    entries = []
    for founder in animals:
        if founder["data"].get("sire_id"):
            continue
        founder_id = founder["id"]
        carriers = [
            {
                "animal_id": animal["id"],
                "name": animal["data"].get("name"),
                "status": animal["status"],
                "contribution": round(contribs[animal["id"]].get(founder_id, 0.0), 6),
            }
            for animal in animals
            if contribs[animal["id"]].get(founder_id, 0.0) > 0
        ]
        carriers.sort(key=lambda item: (-item["contribution"], item["animal_id"]))
        share = (
            sum(contribs[animal["id"]].get(founder_id, 0.0) for animal in living)
            / len(living)
            if living
            else 0.0
        )
        entries.append({
            "founder_id": founder_id,
            "name": founder["data"].get("name"),
            "status": founder["status"],
            "share": round(share, 6),
            "carriers": carriers,
        })
    entries.sort(key=lambda item: (-item["share"], item["founder_id"]))
    return {
        "founders": entries,
        "animal_total": len(animals),
        "living_total": len(living),
    }


def _ensure_founder_overlap(sire, dam, lookup):
    total, top = founder_overlap(sire, dam, lookup)
    if total > FOUNDER_OVERLAP_LIMIT:
        founder = _find_one(lookup, "animal", "id", top[0])
        name = founder["data"].get("name") if founder else None
        label = "%s (%s)" % (name, top[0]) if name else top[0]
        raise ValidationError(
            "founder overlap %.4f exceeds limit %.2f; top shared founder: %s"
            % (total, FOUNDER_OVERLAP_LIMIT, label)
        )
    return total, top


def _validate_pairing_register(actor, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    total, top = _ensure_founder_overlap(sire, dam, lookup)
    extra = {
        "founder_overlap": round(total, 6),
        "founder_overlap_limit": FOUNDER_OVERLAP_LIMIT,
    }
    if top:
        extra["top_shared_founder"] = top[0]
    return extra


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    _ensure_founder_overlap(sire, dam, lookup)
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'animal': _validate_animal, 'pairing': _validate_pairing_register}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by', 'sire_id', 'dam_id'), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

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
        extra = custom(actor, data, lookup) if custom else None
        merged = dict(data)
        if extra:
            merged.update(extra)
        return merged

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
