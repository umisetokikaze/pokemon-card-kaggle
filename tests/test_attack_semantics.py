import unittest

from ptcg_policy.attack_semantics import (
    ATTACK_AURA_JAB,
    ATTACK_COSMIC_BEAM,
    ATTACK_MEGA_BRAVE,
    ATTACK_WILD_PRESS,
    AttackContext,
    can_plan_active_attack,
    evaluate_attack,
)


class AttackSemanticsTests(unittest.TestCase):
    def evaluate(self, attack_id: int, **kwargs):
        result = evaluate_attack(AttackContext(attack_id=attack_id, **kwargs))
        self.assertIsNotNone(result)
        return result

    def test_cosmic_beam_does_nothing_without_lunatone_on_bench(self):
        result = self.evaluate(
            ATTACK_COSMIC_BEAM,
            target_weak_to_fighting=True,
            premium_power_pro_active=True,
        )

        self.assertEqual(0, result.damage)
        self.assertFalse(result.condition_met)

    def test_cosmic_beam_ignores_weakness_and_resistance(self):
        weak = self.evaluate(
            ATTACK_COSMIC_BEAM,
            has_lunatone_on_bench=True,
            target_weak_to_fighting=True,
        )
        resistant = self.evaluate(
            ATTACK_COSMIC_BEAM,
            has_lunatone_on_bench=True,
            target_resists_fighting=True,
        )

        self.assertEqual(70, weak.damage)
        self.assertEqual(70, resistant.damage)

    def test_cosmic_beam_keeps_premium_bonus_before_ignoring_modifiers(self):
        result = self.evaluate(
            ATTACK_COSMIC_BEAM,
            has_lunatone_on_bench=True,
            target_weak_to_fighting=True,
            premium_power_pro_active=True,
        )

        self.assertEqual(100, result.damage)

    def test_wild_press_records_self_damage_and_self_knockout(self):
        result = self.evaluate(ATTACK_WILD_PRESS, attacker_hp=60)

        self.assertEqual(210, result.damage)
        self.assertEqual(70, result.self_damage)
        self.assertTrue(result.self_knockout)

    def test_wild_press_survives_when_hp_exceeds_self_damage(self):
        result = self.evaluate(ATTACK_WILD_PRESS, attacker_hp=71)

        self.assertFalse(result.self_knockout)

    def test_aura_jab_caps_acceleration_at_three_and_needs_a_bench(self):
        no_bench = self.evaluate(
            ATTACK_AURA_JAB,
            discard_basic_fighting=4,
            bench_target_count=0,
        )
        two_energy = self.evaluate(
            ATTACK_AURA_JAB,
            discard_basic_fighting=2,
            bench_target_count=1,
        )
        four_energy = self.evaluate(
            ATTACK_AURA_JAB,
            discard_basic_fighting=4,
            bench_target_count=1,
        )

        self.assertEqual(0, no_bench.aura_attach_count)
        self.assertEqual(2, two_energy.aura_attach_count)
        self.assertEqual(3, four_energy.aura_attach_count)

    def test_premium_bonus_only_applies_to_active_target_and_fighting_attacker(self):
        active = self.evaluate(
            ATTACK_AURA_JAB,
            premium_power_pro_active=True,
        )
        bench = self.evaluate(
            ATTACK_AURA_JAB,
            premium_power_pro_active=True,
            target_is_active=False,
        )
        non_fighting = self.evaluate(
            ATTACK_AURA_JAB,
            premium_power_pro_active=True,
            attacker_is_fighting=False,
        )

        self.assertEqual(160, active.damage)
        self.assertEqual(130, bench.damage)
        self.assertEqual(130, non_fighting.damage)

    def test_premium_bonus_is_applied_before_weakness_and_resistance(self):
        weak = self.evaluate(
            ATTACK_MEGA_BRAVE,
            premium_power_pro_active=True,
            target_weak_to_fighting=True,
        )
        resistant = self.evaluate(
            ATTACK_MEGA_BRAVE,
            premium_power_pro_active=True,
            target_resists_fighting=True,
        )

        self.assertEqual(600, weak.damage)
        self.assertEqual(270, resistant.damage)

    def test_disabled_mega_brave_is_blocked(self):
        result = self.evaluate(ATTACK_MEGA_BRAVE, attack_disabled=True)

        self.assertTrue(result.blocked)
        self.assertEqual(0, result.damage)

    def test_attack_missing_only_for_energy_can_be_planned_before_attachment(self):
        self.assertTrue(
            can_plan_active_attack(
                attack_id=ATTACK_AURA_JAB,
                energy_count=0,
                energy_required=1,
                legal_attack_ids=set(),
            )
        )
        self.assertTrue(
            can_plan_active_attack(
                attack_id=ATTACK_MEGA_BRAVE,
                energy_count=1,
                energy_required=2,
                legal_attack_ids={ATTACK_AURA_JAB},
            )
        )

    def test_fully_powered_attack_missing_from_engine_options_is_not_planned(self):
        self.assertFalse(
            can_plan_active_attack(
                attack_id=ATTACK_MEGA_BRAVE,
                energy_count=2,
                energy_required=2,
                legal_attack_ids={ATTACK_AURA_JAB},
            )
        )

    def test_blocked_attack_is_not_planned_before_attachment(self):
        self.assertFalse(
            can_plan_active_attack(
                attack_id=ATTACK_MEGA_BRAVE,
                energy_count=1,
                energy_required=2,
                legal_attack_ids={ATTACK_AURA_JAB},
                attack_blocked=True,
            )
        )

    def test_unknown_attack_fails_closed_to_caller(self):
        self.assertIsNone(evaluate_attack(AttackContext(attack_id=999_999)))

    def test_negative_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_attack(
                AttackContext(
                    attack_id=ATTACK_AURA_JAB,
                    discard_basic_fighting=-1,
                )
            )


if __name__ == "__main__":
    unittest.main()
