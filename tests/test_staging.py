import copy
import unittest
from unittest.mock import patch

from test_predictive import configuration, fan


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        # Retain regression coverage for the selectable historical allocator.
        self.config['fan_staging'] = {'enabled': True, 'allocation_mode': 'priority',
            'activation_percent': [0, 45, 70]}
        fan.validate_config(self.config)
        self.output = fan.StagedFans()

    def test_primary_cpu_pair_is_selected_and_other_modules_idle(self):
        with patch.object(fan, 'command') as command:
            duties = self.output.apply(self.config, 20, 0)
        self.assertEqual(self.output.raw, {1: 1, 2: 1, 3: 13, 4: 13, 5: 1, 6: 1})
        self.assertEqual(duties['3'], duties['4'])
        self.assertEqual(command.call_args_list[0].args, ('raw', '0x30', '0xf9', '0xff', '0x00'))
        self.assertEqual([call.args[3] for call in command.call_args_list[1:]],
                         ['0x1', '0x2', '0x3', '0x4', '0x5', '0x6'])

    def test_unmodified_outputs_are_not_rewritten_until_periodic_refresh(self):
        with patch.object(fan, 'command') as command:
            self.output.apply(self.config, 27, 0)
            command.reset_mock()
            for now in range(1, 30):
                self.output.apply(self.config, 27, now)
            command.assert_not_called()
            self.output.apply(self.config, 27, 30)
            self.assertEqual(command.call_count, 7)
            self.assertEqual(command.call_args_list[0].args[3:], ('0xff', '0x00'))

    def test_boundary_noise_cannot_toggle_an_adjacent_raw_level(self):
        with patch.object(fan, 'command') as command:
            self.output.apply(self.config, 26.9, 0)
            command.reset_mock()
            for now in range(1, 29):
                self.output.apply(self.config, 27.05 if now % 2 else 26.8, now)
            command.assert_not_called()
            self.assertEqual(self.output.raw[3], 17)

    def test_changes_have_a_hold_time_and_a_small_maximum_step(self):
        with patch.object(fan, 'command') as command:
            self.output.apply(self.config, 20, 0)
            command.reset_mock()
            self.output.apply(self.config, 40, 1)
            command.assert_not_called()
            self.output.apply(self.config, 40, 5)
            self.assertEqual(self.output.raw[3], 15)
            self.assertEqual(self.output.raw[4], 15)
            self.assertEqual(command.call_count, 2)

    def test_secondary_then_outer_pair_join_after_sustained_demand(self):
        with patch.object(fan, 'command'):
            for now in range(11):
                self.output.apply(self.config, 60, now)
            self.assertEqual(self.output.active, [True, True, False])
            self.assertGreater(self.output.raw[2], 1)
            self.assertEqual(self.output.raw[2], self.output.raw[5])
            self.assertEqual(self.output.raw[1], 1)
            self.assertEqual(self.output.raw[6], 1)
            for now in range(11, 22):
                self.output.apply(self.config, 85, now)
            self.assertEqual(self.output.active, [True, True, True])
            self.assertGreater(self.output.raw[1], 1)

    def test_large_demand_jump_still_waits_for_secondary_before_outer_pair(self):
        with patch.object(fan, 'command'):
            for now in range(11):
                self.output.apply(self.config, 85, now)
            self.assertEqual(self.output.active, [True, True, False])
            for now in range(11, 21):
                self.output.apply(self.config, 85, now)
            self.assertEqual(self.output.active, [True, True, True])

    def test_stages_release_slowly_and_pwm_eventually_reaches_idle(self):
        with patch.object(fan, 'command'):
            self.output.apply(self.config, 60, 0, urgent=True)
            self.output.apply(self.config, 20, 1)
            self.output.apply(self.config, 20, 29)
            self.assertTrue(self.output.active[1])
            for now in range(30, 121):
                self.output.apply(self.config, 20, now)
            self.assertEqual(self.output.active, [True, False, False])
            self.assertEqual(self.output.raw[5], 1)
            self.assertEqual(self.output.raw[6], 1)

    def test_emergency_broadcast_bypasses_all_timers_and_quantization(self):
        with patch.object(fan, 'command') as command:
            self.output.apply(self.config, 20, 0)
            command.reset_mock()
            result = self.output.apply(self.config, 100, .1, emergency=True)
            self.assertEqual(set(result.values()), {100})
            self.assertEqual(set(self.output.raw.values()), {63})
            self.assertEqual(command.call_args_list[-1].args, ('raw', '0x30', '0xf9', '0x00', '0x3f'))
            self.assertEqual(command.call_count, 2)

    def test_urgent_heat_enlists_all_pairs_without_waiting(self):
        with patch.object(fan, 'command'):
            self.output.apply(self.config, 1, 0)
            self.output.apply(self.config, 50, .1, urgent=True)
            self.assertEqual(set(self.output.raw.values()), {32})
            self.assertEqual(self.output.active, [True, True, True])

    def test_configuration_edit_retains_known_pwm_and_resets_stage_timers(self):
        with patch.object(fan, 'command'):
            self.output.apply(self.config, 20, 0)
            self.config['fan_staging']['groups'] = [[2, 5], [3, 4], [1, 6]]
            self.output.apply(self.config, 20, 1)
            self.assertEqual(self.output.raw[3], 13)
            self.assertEqual(self.output.raw[2], 1)
            self.output.apply(self.config, 20, 5)
            self.assertEqual(self.output.raw[2], 3)
            self.assertEqual(self.output.raw[3], 11)

    def test_invalid_staging_options_are_rejected_before_hardware_access(self):
        for options in [{'enabled': 'yes'}, {'groups': [[3, 4], [2, 5], [1, 1]]},
                        {'groups': [[True, 4], [2, 5], [3, 6]]},
                        {'activation_percent': [0, 70, 45]},
                        {'output_hold_seconds': 0}, {'output_step_raw': 1.5},
                        {'allocation_mode': 'unknown'}, {'balance_floor_percent': 101},
                        {'priority_bias_percent': -1},
                        {'idle_percent': 0}, {'refresh_seconds': float('nan')}]:
            with self.subTest(options=options):
                config = copy.deepcopy(self.config)
                config['fan_staging'] = options
                with self.assertRaises(ValueError):
                    fan.validate_config(config)
        self.config['cpu_only'] = False
        with self.assertRaisesRegex(ValueError, 'cpu_only'):
            fan.validate_config(self.config)

    def test_delayed_cpu_facing_thermal_model_can_settle_with_other_pairs_idle(self):
        # Qualitative regression only: primary modules have greater CPU effect;
        # it does not predict temperatures on the physical Quanta host.
        control = fan.AdaptiveCPU(40, 0)
        temperature, demand = 40, 1
        airflow = {str(i): 1.0 for i in range(1, 7)}
        history = []
        with patch.object(fan, 'command'):
            for now in range(1, 1501):
                load = 92 if now <= 900 else 0
                demand = control.update(self.config, temperature, demand, now, load)
                values = self.output.apply(self.config, demand, now,
                    emergency=temperature >= 68, urgent=temperature >= 65)
                for i in airflow: airflow[i] += (values[i] - airflow[i]) / 8
                primary = (airflow['3'] + airflow['4']) / 2
                auxiliary = sum(max(0, airflow[i] - 10) for i in ('1', '2', '5', '6'))
                equilibrium = 40 + .30 * load - .4 * max(0, primary - 10) - .05 * auxiliary
                temperature += (equilibrium - temperature) / 45
                history.append((temperature, values))
        steady = history[600:900]
        self.assertTrue(all(58 <= t <= 62 for t, _ in steady))
        self.assertLess(max(t for t, _ in steady) - min(t for t, _ in steady), 2)
        self.assertTrue(all(v[i] == 1.6 for _, v in steady for i in ('1', '2', '5', '6')))
        self.assertLess(max(v['3'] for _, v in steady), 45)
        self.assertLess(history[-1][1]['3'], 5)

class BalancedStagingTests(unittest.TestCase):
    def setUp(self):
        import json
        from pathlib import Path
        self.config = fan.validate_config(json.loads((Path(__file__).resolve().parents[1] /
            'config/quanta-fan-control.staged.json').read_text()))
        self.options = fan.staging_options(self.config)
        self.output = fan.StagedFans()

    def test_neighbours_join_early_and_share_excess_above_pwm_dead_zone(self):
        self.output.targets(self.options, 30, 0)
        targets = self.output.targets(self.options, 30, 5)
        self.assertEqual(self.output.active, [True, True, False])
        self.assertEqual([targets[i] for i in (3, 4, 2, 5, 1, 6)], [26, 26, 24, 24, 1, 1])
        self.assertAlmostEqual((targets[3]-20)+(targets[2]-20), 30-20)

    def test_outer_fans_assist_before_primary_pair_needs_high_duty(self):
        for now in (0, 5, 10):
            targets = self.output.targets(self.options, 40, now)
        self.assertEqual(self.output.active, [True, True, True])
        self.assertLess(targets[3], 30)
        self.assertGreater(targets[1], 20)
        self.assertGreater(targets[3], targets[2])
        self.assertGreater(targets[2], targets[1])
        self.assertAlmostEqual(sum(targets[i]-20 for i in (3, 2, 1)), 20)

    def test_helpers_prime_to_dead_zone_floor_before_primary_cooling_is_reduced(self):
        with patch.object(fan, 'command'):
            self.output.apply(self.config, 30, 0)
            primary = self.output.raw[3]
            self.output.apply(self.config, 30, 5)
            self.assertEqual(self.output.raw[3], primary)
            self.assertEqual(self.output.raw[2], 13)
            self.assertEqual(self.output.raw[5], 13)
            self.output.apply(self.config, 30, 10)
            self.assertEqual(self.output.raw[3], primary-1)
            self.assertEqual(self.output.raw[2], 14)

    def test_ordinary_rpm_changes_stay_small_and_held_after_helpers_are_primed(self):
        with patch.object(fan, 'command'):
            self.output.apply(self.config, 30, 0)
            self.output.apply(self.config, 30, 5)
            before = dict(self.output.raw)
            self.output.apply(self.config, 40, 6)
            for i in (2, 5):
                self.assertEqual(self.output.raw[i], before[i])
            for i in (3, 4):
                self.assertLessEqual(abs(self.output.raw[i]-before[i]), 1)
            self.output.apply(self.config, 40, 10)
            for i in (2, 3, 4, 5):
                self.assertLessEqual(abs(self.output.raw[i]-before[i]), 1)

    def test_stage_boundary_noise_does_not_chatter_and_release_requires_a_minute(self):
        self.output.targets(self.options, 30, 0)
        self.output.targets(self.options, 30, 5)
        for now in range(6, 100):
            self.output.targets(self.options, 21 if now % 2 else 19, now)
            self.assertTrue(self.output.active[1])
        self.output.targets(self.options, 5, 100)
        self.output.targets(self.options, 5, 159)
        self.assertTrue(self.output.active[1])
        self.output.targets(self.options, 5, 160)
        self.assertFalse(self.output.active[1])

    def test_low_demand_after_load_release_can_reach_idle(self):
        with patch.object(fan, 'command'):
            for now in range(0, 101):
                self.output.apply(self.config, 40, now)
            for now in range(101, 321):
                self.output.apply(self.config, 1, now)
        self.assertEqual(set(self.output.raw.values()), {1})
        self.assertEqual(self.output.active, [True, False, False])

    def test_heat_guard_and_faults_override_balancing_and_manual_settings(self):
        with patch.object(fan, 'command') as command:
            self.output.apply(self.config, 1, 0)
            urgent = self.output.apply(self.config, 50, .1, urgent=True, overrides={2: 1})
            self.assertEqual(set(urgent.values()), {50.8})
            self.assertEqual(self.output.active, [True, True, True])
            command.reset_mock()
            emergency = self.output.apply(self.config, 100, .2, emergency=True, overrides={2: 1})
            self.assertEqual(set(emergency.values()), {100})
            self.assertEqual(command.call_count, 2)

    def test_explicit_manual_helper_speed_is_not_primed_or_redistributed(self):
        with patch.object(fan, 'command'):
            self.output.apply(self.config, 30, 0, overrides={2: 1})
            for now in range(1, 31):
                self.output.apply(self.config, 30, now, overrides={2: 1})
        self.assertEqual(self.output.raw[2], 1)
        self.assertGreater(self.output.raw[5], 13)

    def test_balanced_configuration_is_valid_with_the_alternative_cpu_pairs(self):
        self.config['fan_staging']['groups'] = [[2, 4], [3, 5], [1, 6]]
        fan.validate_config(self.config)
        options = fan.staging_options(self.config)
        self.output.targets(options, 30, 0)
        targets = self.output.targets(options, 30, 5)
        self.assertEqual([targets[i] for i in (2, 4, 3, 5)], [26, 26, 24, 24])


if __name__ == '__main__':
    unittest.main()
