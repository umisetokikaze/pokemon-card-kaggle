import unittest

from ptcg_policy.state import AttackPlan, PolicyState


class PolicyStateTests(unittest.TestCase):
    def test_begin_turn_resets_all_turn_local_state_once(self):
        state = PolicyState()
        state.begin_turn(3)
        state.plan = AttackPlan(attacker=1, attack_id=982)
        state.ability_used = True
        state.premium_power_pro_active = True

        state.begin_turn(3)
        self.assertEqual(1, state.plan.attacker)
        self.assertTrue(state.ability_used)

        state.begin_turn(4)
        self.assertEqual(AttackPlan(), state.plan)
        self.assertFalse(state.ability_used)
        self.assertFalse(state.premium_power_pro_active)

    def test_snapshot_and_restore_do_not_alias_attack_plan(self):
        state = PolicyState(turn=5, plan=AttackPlan(attacker=2, damage=270))
        snapshot = state.snapshot()

        state.plan.attacker = 0
        state.plan.damage = 30
        state.ability_used = True
        state.restore(snapshot)

        self.assertEqual(2, state.plan.attacker)
        self.assertEqual(270, state.plan.damage)
        self.assertFalse(state.ability_used)
        self.assertIsNot(state.plan, snapshot.plan)

    def test_reset_match_clears_the_turn_sentinel(self):
        state = PolicyState(
            turn=7,
            plan=AttackPlan(attacker=1),
            ability_used=True,
            premium_power_pro_active=True,
            mega_brave_lock_turn=9,
            mega_brave_locked_serial=42,
        )

        state.reset_match()

        self.assertEqual(-1, state.turn)
        self.assertEqual(AttackPlan(), state.plan)
        self.assertFalse(state.ability_used)
        self.assertFalse(state.premium_power_pro_active)
        self.assertEqual(-1, state.mega_brave_lock_turn)
        self.assertIsNone(state.mega_brave_locked_serial)

    def test_mega_brave_is_locked_only_for_the_same_active_serial_next_turn(self):
        state = PolicyState()
        state.note_mega_brave(turn=5, attacker_serial=42)

        self.assertFalse(state.mega_brave_disabled(turn=5, active_serial=42))
        self.assertTrue(state.mega_brave_disabled(turn=7, active_serial=42))
        self.assertFalse(state.mega_brave_disabled(turn=9, active_serial=42))
        self.assertIsNone(state.mega_brave_locked_serial)

    def test_changed_active_serial_clears_mega_brave_lock(self):
        state = PolicyState()
        state.note_mega_brave(turn=5, attacker_serial=42)

        self.assertFalse(state.mega_brave_disabled(turn=7, active_serial=99))
        self.assertIsNone(state.mega_brave_locked_serial)


if __name__ == "__main__":
    unittest.main()
