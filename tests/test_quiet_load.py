"""Regressions for overshoot and long fan tails in compilation workloads."""
import copy
import json
import unittest
from unittest.mock import patch

from test_predictive import ROOT, fan


class QuietLoadTests(unittest.TestCase):
    def setUp(self):
        self.config = fan.validate_config(json.loads(
            (ROOT / 'config/quanta-fan-control.quiet-load.json').read_text()))

    def test_target_range_and_emergency_margin(self):
        for target in (65, 67, 70):
            config = copy.deepcopy(self.config)
            config['cpu_target_c'] = target
            fan.validate_config(config)
        for target in (71, 76):
            config = copy.deepcopy(self.config)
            config['cpu_target_c'] = target
            with self.assertRaises(ValueError):
                fan.validate_config(config)
        # A low existing endpoint cannot be combined with the new target.
        config = copy.deepcopy(self.config)
        config['cpu_curve'] = [[0, 1], [68, 100]]
        with self.assertRaises(ValueError):
            fan.validate_config(config)

    def test_observed_short_peaks_below_65_do_not_trigger_high_fan_duty(self):
        control = fan.AdaptiveCPU(40, 0)
        duty, peak = 1, 1
        for now in range(1, 601):
            # Two heat bursts resembling the supplied graph; this is a fixed
            # observation replay, not a calibrated thermal response model.
            phase = now % 300
            temperature = 40 + 24 * max(0, 1 - abs(phase - 100) / 60)
            duty = control.update(self.config, temperature, duty, now,
                                  100 if 40 <= phase <= 110 else 0)
            peak = max(peak, duty)
            self.assertLessEqual(duty, 20)
        self.assertGreater(peak, 5)
        self.assertLess(duty, 5)

    def test_sustained_target_band_has_no_upward_trim(self):
        for temperature in (65, 67, 69):
            control = fan.AdaptiveCPU(temperature, 0)
            duty = 1
            for now in range(1, 601):
                duty = control.update(self.config, temperature, duty, now, 100)
            self.assertLessEqual(control.trim, 0)
            self.assertLess(duty, 25)

    def test_hot_feedback_still_increases_airflow_and_emergency_is_immediate(self):
        control = fan.AdaptiveCPU(71, 0)
        duty = 22
        for now in range(1, 181):
            duty = control.update(self.config, 71, duty, now, 100)
        self.assertGreater(control.trim, 0)
        self.assertGreater(duty, 25)
        self.assertEqual(control.update(self.config, 78, duty, 181, 100), 100)

    def test_cold_cpu_releases_excess_trim_even_with_load_still_high(self):
        control = fan.AdaptiveCPU(71, 0)
        duty = 22
        for now in range(1, 301):
            duty = control.update(self.config, 71, duty, now, 100)
        self.assertGreater(control.trim, 15)
        for now in range(301, 331):
            duty = control.update(self.config, 45, duty, now, 100)
        self.assertLessEqual(control.trim, 0)
        self.assertLess(duty, 15)

    def test_controller_and_physical_pwm_recover_from_full_speed_within_a_minute(self):
        control, outputs = fan.AdaptiveCPU(78, 0), fan.StagedFans()
        with patch.object(fan, 'command'):
            duty = control.update(self.config, 78, 1, 1, 100)
            outputs.apply(self.config, duty, 1, emergency=True)
            for now in range(2, 62):
                duty = control.update(self.config, 45, duty, now, 0)
                outputs.apply(self.config, duty, now)
        self.assertLess(duty, 5)
        self.assertLessEqual(max(outputs.raw.values()), 3)
        self.assertEqual(outputs.active, [True, False, False])

    def test_pwm_fall_is_faster_than_rise_without_bypassing_manual_or_emergency(self):
        outputs = fan.StagedFans()
        with patch.object(fan, 'command'):
            outputs.apply(self.config, 1, 0)
            outputs.apply(self.config, 15, 2)
            self.assertEqual(outputs.raw[3], 1)  # rising hold is still five seconds
            outputs.apply(self.config, 15, 5)
            self.assertEqual(outputs.raw[3], 2)
            outputs.apply(self.config, 1, 7)
            self.assertEqual(outputs.raw[3], 1)  # falling hold is two seconds
            outputs.apply(self.config, 1, 8, overrides={2: 35}, force={2})
            self.assertEqual(outputs.raw[2], 23)
            outputs.apply(self.config, 100, 9, emergency=True, overrides={2: 1})
            self.assertEqual(set(outputs.raw.values()), {63})

    def test_invalid_new_slew_options_are_rejected(self):
        for key, value in [('output_fall_step_raw', 1.5),
                           ('output_fall_step_raw', 7), ('output_fall_hold_seconds', 0)]:
            config = copy.deepcopy(self.config)
            config['fan_staging'][key] = value
            with self.assertRaises(ValueError):
                fan.validate_config(config)
        self.config['predictive']['load_release_seconds'] = 0
        with self.assertRaises(ValueError):
            fan.validate_config(self.config)
