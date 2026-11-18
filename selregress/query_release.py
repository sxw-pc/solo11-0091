"""Predeclared release contracts for local synthetic aggregate query cards.

The module reasons only over the finite universe of complete 0/1 worlds for
the persons named by a local HTML snapshot. It deliberately does not treat
individually safe cards as a safe release set, and it never substitutes a
real-valued linear-system feasibility check for 0/1 feasibility.

Two contracts are supported:

``independent_card``
    Review every card on its own. A card may be released only when publishing
    that one card alone leaves every enrolled person with both a 0 and a 1 in
    some complete consistent world. No cross-card safety claim is made.

``composition_safe``
    Choose a card set whose *intersection* constraints leave every person with
    two different complete 0/1 worlds. Total utility is maximised with a
    stable tie-break (fewest cards, then lexicographic query IDs).

Receipts are internal artefacts: they bind the source HTML SHA-256, the
contract and the possible-world certificate. Use
:func:`public_release_artifact` to derive the externally publishable view,
which contains only the approved cards, their members and their counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .util import InputError, canonical_digest

SCHEMA_VERSION = 1
CONTRACT_INDEPENDENT = "independent_card"
CONTRACT_COMPOSITION = "composition_safe"
CONTRACTS = (CONTRACT_INDEPENDENT, CONTRACT_COMPOSITION)
RECEIPT_KIND = "selregress.query_release"
PUBLIC_KIND = "selregress.query_release.public"
MAX_PERSONS = 12


class QueryReleaseError(InputError):
    """Invalid evidence, contract or receipt."""

    def __init__(self, message: str):
        super().__init__("QUERY_RELEASE_INVALID", message)


@dataclass(slots=True, frozen=True)
class _Card:
    query_id: str
    members: tuple[str, ...]
    positive_count: int
    utility: int
    mask: int


@dataclass(slots=True)
class _Model:
    persons: tuple[str, ...]
    world_count: int
    full_mask: int
    one_masks: tuple[int, ...]
    cards: tuple[_Card, ...]

    def safe_mask(self, mask: int) -> bool:
        """Every person has at least one 0-world and one 1-world."""
        if not mask:
            return False
        full = self.full_mask
        for one in self.one_masks:
            if not mask & one or not mask & (full ^ one):
                return False
        return True

    def ambiguous_persons(self, mask: int) -> list[str]:
        full = self.full_mask
        return [
            person for person, one in zip(self.persons, self.one_masks)
            if not (mask & one and mask & (full ^ one))
        ]

    def witness(self, mask: int, index: int) -> dict[str, Any]:
        """Two different worlds for one person, both consistent with *mask*.

        The pair is chosen per person. There is no claim that one global pair
        of worlds differs for everybody at once.
        """
        one = self.one_masks[index]
        zero_world = _mask_index(mask & (self.full_mask ^ one))
        one_world = _mask_index(mask & one)
        return {
            "person": self.persons[index],
            "value_0": 0,
            "value_1": 1,
            "world_0": _assignment(zero_world, self.persons),
            "world_1": _assignment(one_world, self.persons),
        }


def _mask_index(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _assignment(world: int, persons: tuple[str, ...]) -> dict[str, int]:
    return {person: world >> position & 1 for position, person in enumerate(persons)}


def _validated_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise QueryReleaseError("evidence must be a mapping")
    persons = evidence.get("persons")
    queries = evidence.get("queries")
    if (not isinstance(persons, list) or not 3 <= len(persons) <= MAX_PERSONS
            or len(set(persons)) != len(persons)
            or not all(isinstance(item, str) and item for item in persons)):
        raise QueryReleaseError("invalid person universe")
    if not isinstance(queries, list) or len(queries) < 3:
        raise QueryReleaseError("too few aggregate queries")
    known = set(persons)
    seen: set[str] = set()
    for row in queries:
        if not isinstance(row, dict):
            raise QueryReleaseError("invalid query card")
        query_id = row.get("id")
        members = row.get("members")
        count = row.get("positive_count")
        utility = row.get("utility")
        if (not isinstance(query_id, str) or not query_id or query_id in seen
                or not isinstance(members, list) or len(members) < 2
                or len(set(members)) != len(members) or not set(members) <= known
                or not isinstance(count, int) or isinstance(count, bool)
                or not isinstance(utility, int) or isinstance(utility, bool)
                or count < 0 or count > len(members) or utility <= 0):
            raise QueryReleaseError("duplicate or invalid query card")
        seen.add(query_id)
    return evidence


def _build_model(evidence: dict[str, Any]) -> _Model:
    persons = tuple(evidence["persons"])
    index = {person: position for position, person in enumerate(persons)}
    world_count = 1 << len(persons)
    full_mask = (1 << world_count) - 1
    one_masks = [0] * len(persons)
    for world in range(world_count):
        bit = 1 << world
        for position in range(len(persons)):
            if world >> position & 1:
                one_masks[position] |= bit
    cards: list[_Card] = []
    for row in evidence["queries"]:
        positions = tuple(index[person] for person in row["members"])
        mask = 0
        for world in range(world_count):
            if sum(world >> position & 1 for position in positions) == row["positive_count"]:
                mask |= 1 << world
        cards.append(_Card(
            query_id=row["id"], members=tuple(row["members"]),
            positive_count=row["positive_count"], utility=row["utility"], mask=mask,
        ))
    return _Model(persons, world_count, full_mask, tuple(one_masks), tuple(cards))


def _card_view(card: _Card) -> dict[str, Any]:
    return {
        "id": card.query_id,
        "members": list(card.members),
        "positive_count": card.positive_count,
        "utility": card.utility,
    }


def _independent_decision(model: _Model) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    approved: list[str] = []
    withheld: list[str] = []
    for card in model.cards:
        safe = model.safe_mask(card.mask)
        review = {
            **_card_view(card),
            "decision": "release" if safe else "withhold",
            "individually_safe": safe,
            "consistent_worlds": card.mask.bit_count(),
            "witnesses": ([model.witness(card.mask, position)
                           for position in range(len(model.persons))] if safe else []),
        }
        if not safe:
            review["exposed_persons"] = model.ambiguous_persons(card.mask)
            withheld.append(card.query_id)
        else:
            approved.append(card.query_id)
        reviews.append(review)
    utilities = {card.query_id: card.utility for card in model.cards}
    return {
        "contract": CONTRACT_INDEPENDENT,
        "cross_card_safety_claimed": False,
        "approved_query_ids": approved,
        "withheld_query_ids": withheld,
        "approved_utility_total": sum(utilities[query_id] for query_id in approved),
        "card_reviews": reviews,
    }


def _prefer(utility: int, ids: list[str], best: dict[str, Any]) -> bool:
    if utility != best["utility"]:
        return utility > best["utility"]
    if len(ids) != len(best["ids"]):
        return len(ids) < len(best["ids"])
    return tuple(sorted(ids)) < tuple(best["ids"])


def _composition_decision(model: _Model) -> dict[str, Any]:
    order = sorted(model.cards, key=lambda card: (-card.utility, card.query_id))
    count = len(order)
    suffix_utility = [0] * (count + 1)
    suffix_mask = [model.full_mask] * (count + 1)
    for position in range(count - 1, -1, -1):
        suffix_utility[position] = suffix_utility[position + 1] + order[position].utility
        suffix_mask[position] = suffix_mask[position + 1] & order[position].mask

    # Releasing nothing leaves every person with all 2**n worlds, so the empty
    # set is the always-available safe fallback with utility 0.
    best: dict[str, Any] = {"utility": 0, "ids": [], "mask": model.full_mask}
    stats = {"explored_nodes": 0, "pruned_by_utility_bound": 0,
             "pruned_by_safety": 0, "closed_remainder": 0}
    selected: list[_Card] = []

    def record(utility: int, ids: list[str], mask: int) -> None:
        nonlocal best
        if model.safe_mask(mask) and _prefer(utility, ids, best):
            best = {"utility": utility, "ids": sorted(ids), "mask": mask}

    def search(position: int, mask: int, utility: int) -> None:
        stats["explored_nodes"] += 1
        current_ids = [card.query_id for card in selected]
        record(utility, current_ids, mask)
        if position == count:
            return
        if utility + suffix_utility[position] < best["utility"]:
            stats["pruned_by_utility_bound"] += 1
            return
        remainder = mask & suffix_mask[position]
        if model.safe_mask(remainder):
            # Every remaining utility is positive, so including all of them is
            # the unique utility maximum of this subtree.
            record(utility + suffix_utility[position],
                   current_ids + [card.query_id for card in order[position:]], remainder)
            stats["closed_remainder"] += 1
            return
        card = order[position]
        narrowed = mask & card.mask
        if model.safe_mask(narrowed):
            selected.append(card)
            search(position + 1, narrowed, utility + card.utility)
            selected.pop()
        else:
            stats["pruned_by_safety"] += 1
        search(position + 1, mask, utility)

    search(0, model.full_mask, 0)
    approved_ids = best["ids"]
    approved_mask = best["mask"]
    utilities = {card.query_id: card.utility for card in model.cards}
    approved = set(approved_ids)
    return {
        "contract": CONTRACT_COMPOSITION,
        "cross_card_safety_claimed": True,
        "approved_query_ids": approved_ids,
        "withheld_query_ids": [card.query_id for card in model.cards
                               if card.query_id not in approved],
        "approved_utility_total": sum(utilities[query_id] for query_id in approved_ids),
        "card_reviews": [
            {
                **_card_view(card),
                "decision": "release" if card.query_id in approved else "withhold",
            }
            for card in model.cards
        ],
        "joint_witnesses": [model.witness(approved_mask, position)
                            for position in range(len(model.persons))],
        "consistent_worlds": approved_mask.bit_count(),
        "optimality_certificate": {
            "method": "exhaustive_branch_and_bound",
            "search_complete": True,
            "world_universe_size": model.world_count,
            "query_count": count,
            "utility_upper_bound": suffix_utility[0],
            "optimal_utility": sum(utilities[query_id] for query_id in approved_ids),
            **stats,
        },
    }


def decide_query_release(evidence: Any, contract: str) -> dict[str, Any]:
    """Return an internal release receipt for one predeclared contract.

    ``evidence`` is the mapping produced by
    :func:`selregress.query_privacy_evidence.inspect_query_snapshot`, or an
    equivalent local mapping with ``persons`` and ``queries``.
    """
    if contract not in CONTRACTS:
        raise QueryReleaseError("unknown release contract")
    checked = _validated_evidence(evidence)
    model = _build_model(checked)
    decision = (_independent_decision(model) if contract == CONTRACT_INDEPENDENT
                else _composition_decision(model))
    receipt: dict[str, Any] = {
        "kind": RECEIPT_KIND,
        "schema_version": SCHEMA_VERSION,
        "internal_only": True,
        "publishable": False,
        "contract": contract,
        "release_id": checked.get("release_id"),
        "snapshot_path": checked.get("snapshot_path", ""),
        "snapshot_sha256": checked.get("snapshot_sha256", ""),
        "persons": list(model.persons),
        "query_ids": [card.query_id for card in model.cards],
        "cards": [_card_view(card) for card in model.cards],
        # Aggregate 0/1 feasibility is exact; real-valued linear relaxations
        # are deliberately never consulted.
        "feasibility_basis": "exhaustive_binary_worlds",
        "world_universe_size": model.world_count,
        **decision,
    }
    receipt["decision_sha256"] = canonical_digest(
        {key: value for key, value in receipt.items() if key != "decision_sha256"})
    return receipt


def _checked_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("kind") != RECEIPT_KIND:
        raise QueryReleaseError("not a query release receipt")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise QueryReleaseError("unsupported receipt schema version")
    if receipt.get("contract") not in CONTRACTS:
        raise QueryReleaseError("unknown release contract")
    if receipt.get("internal_only") is not True or receipt.get("publishable") is not False:
        raise QueryReleaseError("receipt must remain internal and unpublished")
    expected = receipt.get("decision_sha256")
    payload = {key: value for key, value in receipt.items() if key != "decision_sha256"}
    if not isinstance(expected, str) or canonical_digest(payload) != expected:
        raise QueryReleaseError("receipt digest mismatch")
    cards = receipt.get("cards")
    if not isinstance(cards, list) or not cards:
        raise QueryReleaseError("invalid receipt cards")
    card_ids = []
    for card in cards:
        if (not isinstance(card, dict) or not isinstance(card.get("id"), str)
                or not card["id"]
                or not isinstance(card.get("members"), list)
                or len(card["members"]) < 2
                or not all(isinstance(item, str) and item for item in card["members"])
                or len(set(card["members"])) != len(card["members"])
                or not isinstance(card.get("positive_count"), int)
                or isinstance(card["positive_count"], bool)
                or not 0 <= card["positive_count"] <= len(card["members"])
                or not isinstance(card.get("utility"), int)
                or isinstance(card["utility"], bool) or card["utility"] <= 0):
            raise QueryReleaseError("invalid receipt cards")
        card_ids.append(card["id"])
    if len(set(card_ids)) != len(card_ids):
        raise QueryReleaseError("invalid receipt cards")
    persons = receipt.get("persons")
    if (not isinstance(persons, list) or not persons
            or not all(isinstance(item, str) and item for item in persons)
            or any(not set(card["members"]) <= set(persons) for card in cards)):
        raise QueryReleaseError("invalid receipt person universe")
    approved = receipt.get("approved_query_ids")
    withheld = receipt.get("withheld_query_ids")
    if (not isinstance(approved, list) or not isinstance(withheld, list)
            or not all(isinstance(item, str) for item in approved + withheld)
            or set(approved) & set(withheld)
            or set(approved) | set(withheld) != set(card_ids)):
        raise QueryReleaseError("invalid approved or withheld query IDs")
    utilities = {card["id"]: card["utility"] for card in cards}
    if receipt.get("approved_utility_total") != sum(utilities[item] for item in approved):
        raise QueryReleaseError("receipt utility total mismatch")
    return receipt


def verify_query_receipt(
    receipt: Any,
    evidence: dict[str, Any] | None = None,
    *,
    expected_contract: str | None = None,
) -> None:
    """Verify receipt integrity and, optionally, its binding to evidence.

    Raises :class:`QueryReleaseError` on any mismatch. The receipt's own
    possible-world certificate is recomputed from the binding evidence when
    supplied.
    """
    checked = _checked_receipt(receipt)
    contract = checked["contract"]
    if expected_contract is not None and contract != expected_contract:
        raise QueryReleaseError("receipt was issued under a different contract")
    if contract == CONTRACT_INDEPENDENT and checked.get("cross_card_safety_claimed") is not False:
        raise QueryReleaseError("independent receipt claims cross-card safety")
    if contract == CONTRACT_COMPOSITION and checked.get("cross_card_safety_claimed") is not True:
        raise QueryReleaseError("composition receipt lacks cross-card claim")
    if evidence is None:
        return
    bound = _validated_evidence(evidence)
    persons = list(bound["persons"])
    query_ids = [row["id"] for row in bound["queries"]]
    if checked.get("persons") != persons or checked.get("query_ids") != query_ids:
        raise QueryReleaseError("receipt does not match evidence universe")
    if checked.get("release_id") != bound.get("release_id"):
        raise QueryReleaseError("receipt does not match release identity")
    if checked.get("snapshot_sha256") != bound.get("snapshot_sha256", ""):
        raise QueryReleaseError("receipt does not match source snapshot")
    recomputed = decide_query_release(bound, contract)
    for key in ("approved_query_ids", "withheld_query_ids", "approved_utility_total",
                "card_reviews", "joint_witnesses", "consistent_worlds",
                "optimality_certificate", "world_universe_size", "feasibility_basis"):
        if checked.get(key) != recomputed.get(key):
            raise QueryReleaseError("receipt decision is not reproducible")


def public_release_artifact(
    receipt: Any,
    *,
    expected_contract: str | None = None,
) -> dict[str, Any]:
    """Strip internal witnesses and the source digest for publication.

    Only approved cards, their members and their positive counts leave the
    process. Withheld counts, possible worlds, witnesses, the source HTML
    digest and the internal decision digest never appear in the result.
    """
    checked = _checked_receipt(receipt)
    if expected_contract is not None and checked["contract"] != expected_contract:
        raise QueryReleaseError("receipt was issued under a different contract")
    by_id = {card["id"]: card for card in checked["cards"]}
    return {
        "kind": PUBLIC_KIND,
        "schema_version": SCHEMA_VERSION,
        "queries": [
            {
                "id": by_id[query_id]["id"],
                "members": list(by_id[query_id]["members"]),
                "positive_count": by_id[query_id]["positive_count"],
            }
            for query_id in sorted(checked["approved_query_ids"])
        ],
    }
