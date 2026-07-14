"""Pure attack and effect semantics for the Mega Lucario deck.

The constants and card text represented here are sourced from the CABT engine's
``all_card_data()`` and ``all_attack()`` metadata.  Keeping this module free of
``cg`` imports makes the edge cases cheap to unit test on Windows.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final


ATTACK_WILD_PRESS: Final = 978
ATTACK_COSMIC_BEAM: Final = 980
ATTACK_AURA_JAB: Final = 982
ATTACK_MEGA_BRAVE: Final = 983

PREMIUM_POWER_PRO_BONUS: Final = 30
FIGHTING_RESISTANCE_REDUCTION: Final = 30


@dataclass(frozen=True)
class AttackSpec:
    """Stable, deck-local facts for one supported attack."""

    attack_id: int
    name: str
    base_damage: int
    energy_required: int
    self_damage: int = 0
    ignores_weakness_and_resistance: bool = False
    requires_lunatone_on_bench: bool = False
    aura_attach_cap: int = 0


ATTACK_SPECS: Final[dict[int, AttackSpec]] = {
    ATTACK_WILD_PRESS: AttackSpec(
        attack_id=ATTACK_WILD_PRESS,
        name="Wild Press",
        base_damage=210,
        energy_required=3,
        self_damage=70,
    ),
    ATTACK_COSMIC_BEAM: AttackSpec(
        attack_id=ATTACK_COSMIC_BEAM,
        name="Cosmic Beam",
        base_damage=70,
        energy_required=1,
        ignores_weakness_and_resistance=True,
        requires_lunatone_on_bench=True,
    ),
    ATTACK_AURA_JAB: AttackSpec(
        attack_id=ATTACK_AURA_JAB,
        name="Aura Jab",
        base_damage=130,
        energy_required=1,
        aura_attach_cap=3,
    ),
    ATTACK_MEGA_BRAVE: AttackSpec(
        attack_id=ATTACK_MEGA_BRAVE,
        name="Mega Brave",
        base_damage=270,
        energy_required=2,
    ),
}


@dataclass(frozen=True)
class AttackContext:
    """State needed to resolve the supported damage and immediate effects."""

    attack_id: int
    attacker_is_fighting: bool = True
    target_is_active: bool = True
    target_weak_to_fighting: bool = False
    target_resists_fighting: bool = False
    premium_power_pro_active: bool = False
    has_lunatone_on_bench: bool = False
    discard_basic_fighting: int = 0
    bench_target_count: int = 0
    attack_disabled: bool = False
    attacker_hp: int | None = None


@dataclass(frozen=True)
class AttackEvaluation:
    """Resolved values used by the greedy planner."""

    spec: AttackSpec
    damage: int
    self_damage: int
    aura_attach_count: int
    blocked: bool
    condition_met: bool
    self_knockout: bool


def can_plan_active_attack(
    *,
    attack_id: int,
    energy_count: int,
    energy_required: int,
    legal_attack_ids: Collection[int],
    attack_blocked: bool = False,
) -> bool:
    """Keep attacks that can become legal after this turn's attachment.

    The engine only exposes attacks legal at the current selection.  A missing
    option is therefore conclusive only when the attacker already has enough
    Energy; otherwise the planner may still attach one Energy first.
    """

    if energy_count < 0 or energy_required < 0:
        raise ValueError("Energy counts must be non-negative.")
    if attack_blocked:
        return False
    return energy_count < energy_required or attack_id in legal_attack_ids


def evaluate_attack(context: AttackContext) -> AttackEvaluation | None:
    """Resolve a supported attack, returning ``None`` for unknown attacks.

    The native engine remains the source of legality.  ``attack_disabled`` is
    exposed for deterministic tests and future search states; the live agent
    also relies on the engine omitting disabled ATTACK options.
    """

    if context.discard_basic_fighting < 0 or context.bench_target_count < 0:
        raise ValueError("Card and target counts must be non-negative.")
    if context.attacker_hp is not None and context.attacker_hp < 0:
        raise ValueError("Attacker HP must be non-negative.")

    spec = ATTACK_SPECS.get(context.attack_id)
    if spec is None:
        return None

    blocked = context.attack_disabled
    condition_met = not spec.requires_lunatone_on_bench or context.has_lunatone_on_bench
    if blocked or not condition_met:
        damage = 0
    else:
        damage = spec.base_damage
        if (
            context.premium_power_pro_active
            and context.attacker_is_fighting
            and context.target_is_active
        ):
            damage += PREMIUM_POWER_PRO_BONUS

        if not spec.ignores_weakness_and_resistance:
            if context.target_weak_to_fighting:
                damage *= 2
            elif context.target_resists_fighting:
                damage = max(0, damage - FIGHTING_RESISTANCE_REDUCTION)

    self_damage = spec.self_damage if not blocked else 0
    aura_attach_count = 0
    if not blocked and spec.aura_attach_cap and context.bench_target_count:
        aura_attach_count = min(
            spec.aura_attach_cap,
            context.discard_basic_fighting,
        )

    return AttackEvaluation(
        spec=spec,
        damage=damage,
        self_damage=self_damage,
        aura_attach_count=aura_attach_count,
        blocked=blocked,
        condition_met=condition_met,
        self_knockout=(
            context.attacker_hp is not None
            and self_damage >= context.attacker_hp
            and self_damage > 0
        ),
    )
