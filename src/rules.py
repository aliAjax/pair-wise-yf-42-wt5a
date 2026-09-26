from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


#: 双亲奠基血缘重合度上限，超过则配对建议不允许进入待审。
FOUNDER_OVERLAP_LIMIT = 0.25

#: 隔离中的个体仍算存活个体，但只有 active 个体可以进入新配对建议。
LIVING_ANIMAL_STATUSES = ("active", "quarantined")


def _parent_id(animal, field):
    value = (animal.get("data") or {}).get(field)
    return value if value not in (None, "") else None


def _is_founder(animal):
    """档案里没写父本的个体按奠基个体算。"""
    return _parent_id(animal, "sire_id") is None


def founder_contributions(animal_id, by_id, memo=None, seen=None):
    """顺着父母链回算奠基个体贡献占比。

    奠基个体对自身贡献 1.0；非奠基个体取父本、母本贡献向量各一半。
    返回 ``{奠基个体id: 占比}``；父/母缺失或指向不存在的档案时，
    对应一半血缘无据可查，不计入任何奠基个体。
    """
    if memo is None:
        memo = {}
    if animal_id in memo:
        return memo[animal_id]
    seen = set(seen or ())
    if animal_id in seen:
        # 谱系环属于脏数据，回算到环处截断，避免无限递归。
        return {}
    seen = seen | {animal_id}
    animal = by_id.get(animal_id)
    if animal is None or _is_founder(animal):
        memo[animal_id] = {animal_id: 1.0}
        return memo[animal_id]
    contributions = {}
    for field in ("sire_id", "dam_id"):
        parent_id = _parent_id(animal, field)
        if parent_id is None or parent_id not in by_id:
            continue
        for founder_id, share in founder_contributions(
            parent_id, by_id, memo, seen
        ).items():
            contributions[founder_id] = contributions.get(founder_id, 0.0) + share / 2
    memo[animal_id] = contributions
    return contributions


def founder_overlap(sire, dam, by_id):
    """两只个体的奠基血缘重合度：逐奠基者取双方占比的较小值后求和。

    返回总体重合度以及按重合度排序的明细，用于指出重合最多的奠基个体。
    """
    sire_contrib = founder_contributions(sire["id"], by_id)
    dam_contrib = founder_contributions(dam["id"], by_id)
    shared = []
    for founder_id in set(sire_contrib) & set(dam_contrib):
        shared.append(
            {
                "founder_id": founder_id,
                "overlap": round(
                    min(sire_contrib[founder_id], dam_contrib[founder_id]), 6
                ),
                "sire_contribution": round(sire_contrib[founder_id], 6),
                "dam_contribution": round(dam_contrib[founder_id], 6),
            }
        )
    shared.sort(key=lambda item: (-item["overlap"], item["founder_id"]))
    return round(sum(item["overlap"] for item in shared), 6), shared


def build_founder_ledger(animals):
    """汇总每只奠基个体在存活个体中的平均血缘占比。

    存活 = active 或 quarantined；已去世/隔离个体的占比照常展示。
    返回的奠基者按占比降序排列，携带个体按贡献占比降序列出。
    """
    by_id = {animal["id"]: animal for animal in animals}
    memo = {}
    living = [a for a in animals if a["status"] in LIVING_ANIMAL_STATUSES]

    # 奠基者：档案中存在且没写父本的个体；缺失档案的悬空父母不列入台账。
    founders = {a["id"]: a for a in animals if _is_founder(a)}
    totals = {founder_id: 0.0 for founder_id in founders}
    carriers = {founder_id: [] for founder_id in founders}

    for animal in living:
        for founder_id, share in founder_contributions(animal["id"], by_id, memo).items():
            if founder_id not in totals:
                continue
            totals[founder_id] += share
            carriers[founder_id].append((share, animal))

    living_count = len(living)
    entries = []
    for founder_id, founder in founders.items():
        total = totals[founder_id]
        carrying = sorted(carriers[founder_id], key=lambda item: (-item[0], item[1]["id"]))
        entries.append(
            {
                "founder_id": founder_id,
                "founder_name": founder["data"].get("name") or founder_id,
                "founder_status": founder["status"],
                "share": round(total / living_count, 6) if living_count else 0.0,
                "carrier_count": len(carrying),
                "carriers": [
                    {
                        "animal_id": animal["id"],
                        "animal_name": animal["data"].get("name") or animal["id"],
                        "status": animal["status"],
                        "contribution": round(share, 6),
                    }
                    for share, animal in carrying
                ],
            }
        )
    entries.sort(key=lambda entry: (-entry["share"], entry["founder_id"]))
    return {"living_count": living_count, "founders": entries}


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


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _find_all(lookup, kind):
    if lookup is None:
        return []
    rows = lookup(kind, None, None)
    return rows or []


def pair_overlap_preview(sire_id, dam_id, by_id):
    """只读试算两只个体的奠基血缘重合度，不产生配对记录。"""
    sire = by_id.get(sire_id)
    dam = by_id.get(dam_id)
    if sire is None or dam is None:
        raise ValidationError("pairing requires two existing animals")
    if sire.get("kind") != "animal" or dam.get("kind") != "animal":
        raise ValidationError("pairing requires two existing animals")
    if sire_id == dam_id:
        raise ValidationError("sire and dam must be different animals")
    overlap, shared = founder_overlap(sire, dam, by_id)
    return {
        "sire_id": sire_id,
        "dam_id": dam_id,
        "sire_status": sire["status"],
        "dam_status": dam["status"],
        "both_active": sire["status"] == "active" and dam["status"] == "active",
        "founder_overlap": overlap,
        "limit": FOUNDER_OVERLAP_LIMIT,
        "within_limit": overlap <= FOUNDER_OVERLAP_LIMIT,
        "top_shared_founder_id": shared[0]["founder_id"] if shared else None,
        "top_shared_overlap": shared[0]["overlap"] if shared else 0.0,
        "founder_overlap_detail": shared,
    }


def _validate_pairing_create(actor, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["id"] == dam["id"]:
        raise ValidationError("sire and dam must be different animals")
    # 隔离或已去世的个体不能再进新建议（占比仍在台账中照常显示）。
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    by_id = {animal["id"]: animal for animal in _find_all(lookup, "animal")}
    preview = pair_overlap_preview(sire["id"], dam["id"], by_id)
    overlap = preview["founder_overlap"]
    shared = preview["founder_overlap_detail"]
    if not preview["within_limit"]:
        top = shared[0]
        top_founder = by_id.get(top["founder_id"])
        top_name = (
            (top_founder["data"].get("name") or top["founder_id"])
            if top_founder
            else top["founder_id"]
        )
        raise ValidationError(
            "founder overlap %s exceeds limit %s; most shared founder is %s (%s) at %s"
            % (overlap, FOUNDER_OVERLAP_LIMIT, top["founder_id"], top_name, top["overlap"])
        )
    return {
        "founder_overlap": overlap,
        "founder_overlap_detail": shared,
        "top_shared_founder_id": preview["top_shared_founder_id"],
        "top_shared_overlap": preview["top_shared_overlap"],
    }


def _validate_pairing(actor, entity, data, lookup):
    sire_id = data.get("sire_id") or (entity.get("data") or {}).get("sire_id")
    dam_id = data.get("dam_id") or (entity.get("data") or {}).get("dam_id")
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'animal': _validate_animal, 'pairing': _validate_pairing_create}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by', 'sire_id', 'dam_id'), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('approvals',), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
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
        extra = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if extra:
            payload.update(extra)
        return payload

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


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
