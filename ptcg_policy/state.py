"""Explicit match and turn state for the Mega Lucario policy."""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass
class AttackPlan:
    """Best currently projected attack sequence."""

    attacker: int = -1
    target: int = -1
    attack_id: int = -1
    remain_hp: int = -1
    energy: bool = False
    damage: int = 0
    self_damage: int = 0
    aura_attach_count: int = 0
    premium_damage_gain: int = 0
    premium_enables_knockout: bool = False


@dataclass(frozen=True)
class PolicySnapshot:
    """Copyable state boundary for future virtual-search branches."""

    turn: int
    plan: AttackPlan
    ability_used: bool
    premium_power_pro_active: bool
    mega_brave_lock_turn: int
    mega_brave_locked_serial: int | None


@dataclass
class PolicyState:
    """State owned by one real match, with turn-local fields reset together."""

    turn: int = -1
    plan: AttackPlan = field(default_factory=AttackPlan)
    ability_used: bool = False
    premium_power_pro_active: bool = False
    mega_brave_lock_turn: int = -1
    mega_brave_locked_serial: int | None = None

    def begin_turn(self, turn: int) -> None:
        if turn == self.turn:
            return
        self.turn = turn
        self.plan = AttackPlan()
        self.ability_used = False
        self.premium_power_pro_active = False

    def reset_match(self) -> None:
        self.turn = -1
        self.plan = AttackPlan()
        self.ability_used = False
        self.premium_power_pro_active = False
        self.clear_mega_brave_lock()

    def note_mega_brave(self, turn: int, attacker_serial: int) -> None:
        """Remember the next own turn on which the active attacker is locked."""

        self.mega_brave_lock_turn = turn + 2
        self.mega_brave_locked_serial = attacker_serial

    def clear_mega_brave_lock(self) -> None:
        self.mega_brave_lock_turn = -1
        self.mega_brave_locked_serial = None

    def mega_brave_disabled(self, turn: int, active_serial: int | None) -> bool:
        """Return whether the same still-active Pokémon is in its lock turn.

        Moving to the Bench clears effects on that Pokémon.  A changed active
        serial therefore clears the remembered lock instead of transferring it.
        """

        if self.mega_brave_locked_serial is None:
            return False
        if active_serial != self.mega_brave_locked_serial:
            self.clear_mega_brave_lock()
            return False
        if turn > self.mega_brave_lock_turn:
            self.clear_mega_brave_lock()
            return False
        return turn == self.mega_brave_lock_turn

    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(
            turn=self.turn,
            plan=replace(self.plan),
            ability_used=self.ability_used,
            premium_power_pro_active=self.premium_power_pro_active,
            mega_brave_lock_turn=self.mega_brave_lock_turn,
            mega_brave_locked_serial=self.mega_brave_locked_serial,
        )

    def restore(self, snapshot: PolicySnapshot) -> None:
        self.turn = snapshot.turn
        self.plan = replace(snapshot.plan)
        self.ability_used = snapshot.ability_used
        self.premium_power_pro_active = snapshot.premium_power_pro_active
        self.mega_brave_lock_turn = snapshot.mega_brave_lock_turn
        self.mega_brave_locked_serial = snapshot.mega_brave_locked_serial
