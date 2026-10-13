import copy
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fan', ROOT / 'quanta-fan-control.py')
fan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fan)


def configuration():
    config = json.loads((ROOT / 'config/quanta-fan-control.base.json').read_text())
    config.update(cpu_control_mode='predictive', cpu_target_c=60,
                  cpu_curve=[[0, 1], [45, 1], [50, 5], [60, 10],
                             [65, 15], [66, 17], [67, 20], [68, 100]])
    return fan.validate_config(config)


class LoadTests(unittest.TestCase):
    def test_guest_is_not_double_counted_and_wait_is_not_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stat'
            sampler = fan.CPULoad(str(path))
            path.write_text('cpu 100 10 20 100 10 0 0 0 80 5\ncpu0 0\ncpu1 0\n')
            self.assertIsNone(sampler.sample())
            path.write_text('cpu 180 10 30 190 25 2 3 0 160 5\ncpu0 0\ncpu1 0\n')
            self.assertAlmostEqual(sampler.sample(), 100 * 95 / 200)
            self.assertTrue(sampler.available)

    def test_bad_counters_hotplug_and_reset_establish_a_new_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stat'
            sampler = fan.CPULoad(str(path))
            for content in ['bad', 'cpu 1 2\ncpu0 0',
                            'cpu 10 0 0 10 0 0 0 0\ncpu0 0',
                            'cpu 1 0 0 1 0 0 0 0\ncpu0 0',
                            'cpu 2 0 0 2 0 0 0 0\ncpu0 0\ncpu1 0']:
                path.write_text(content)
                self.assertIsNone(sampler.sample())
            path.write_text('cpu 3 0 0 3 0 0 0 0\ncpu0 0\ncpu1 0')
            self.assertEqual(sampler.sample(), 50)
            path.unlink()
            self.assertIsNone(sampler.sample())

    def test_iowait_regression_does_not_create_negative_utilization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stat'
            sampler = fan.CPULoad(str(path))
            path.write_text('cpu 1 0 0 1 20 0 0 0\ncpu0 0')
            sampler.sample()
            path.write_text('cpu 11 0 0 11 10 0 0 0\ncpu0 0')
            self.assertEqual(sampler.sample(), 50)


class PredictiveTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()

    def run_constant(self, temperature, load, duration=300, duty=1):
        control = fan.AdaptiveCPU(temperature, 0)
        values = []
        for now in range(1, duration + 1):
            duty = control.update(self.config, temperature, duty, now, load)
            values.append(duty)
        return control, values

    def test_load_provides_airflow_before_temperature_rises(self):
        control, values = self.run_constant(45, 92, duration=20)
        self.assertGreater(values[-1], 15)
        self.assertGreater(control.predicted, control.filtered)
        self.assertTrue(all(b - a <= 1.00001 for a, b in zip(values, values[1:])))

    def test_short_load_burst_is_slew_limited(self):
        control = fan.AdaptiveCPU(40, 0)
        duty = 1
        for now in range(1, 4):
            duty = control.update(self.config, 40, duty, now, 100)
        self.assertLessEqual(duty, 4)

    def test_target_band_does_not_accumulate_error(self):
        control, values = self.run_constant(61, 92)
        self.assertEqual(control.trim, 0)
        self.assertLess(max(values), 30)
        self.assertGreater(min(values[-60:]), 20)

    def test_hot_trim_changes_only_after_settling_and_is_bounded(self):
        control, values = self.run_constant(62, 92, duration=900)
        self.assertLessEqual(control.trim, fan.PREDICTIVE_DEFAULTS['max_trim_percent'])
        self.assertLess(values[29], 32)
        self.assertLess(max(values), 60)

    def test_cooling_stops_positive_prediction_and_hot_trim(self):
        control = fan.AdaptiveCPU(60, 0)
        duty = 25
        for now in range(1, 31):
            duty = control.update(self.config, 62, duty, now, 92)
        trim = control.trim
        control.update(self.config, 60, duty, 31, 92)
        self.assertEqual(control.trim, trim)
        self.assertAlmostEqual(control.predicted, control.filtered, places=3)

    def test_negative_trim_can_find_airflow_below_provisional_load_curve(self):
        control, values = self.run_constant(55, 92, duration=200, duty=27)
        self.assertLess(control.trim, 0)
        self.assertGreater(min(values), 5)
        self.assertLess(values[-1], control.load_duty)

    def test_gradual_new_load_does_not_reuse_idle_cooling_trim(self):
        control, values = self.run_constant(40, 25, duration=150)
        self.assertLess(control.trim, 0)
        duty = values[-1]
        for now in range(151, 172):
            duty = control.update(self.config, 40, duty, now, min(100, 25 + (now - 150) * 4))
        self.assertGreaterEqual(control.trim, 0)

    def test_cooling_correction_cannot_wind_up_below_curve_floor(self):
        control, values = self.run_constant(40, 25, duration=900)
        self.assertAlmostEqual(control.trim, control.curve_duty - control.load_duty)
        self.assertGreaterEqual(min(values), 1)

    def test_integer_sensor_jitter_does_not_disable_temperature_feedback(self):
        for base, direction in [(61, 1), (55, -1)]:
            with self.subTest(base=base):
                control = fan.AdaptiveCPU(base, 0)
                duty = 26
                for now in range(1, 241):
                    duty = control.update(self.config, base + now % 2, duty, now, 92)
                self.assertGreater(direction * control.trim, 0)

    def test_prediction_cannot_select_full_speed_point_without_raw_heat(self):
        control = fan.AdaptiveCPU(64, 0)
        control.rate = 5
        duty = control.update(self.config, 64.1, 20, 1, 100)
        self.assertLess(control.curve_duty, 100)
        self.assertLess(duty, 100)

    def test_load_release_does_not_drop_airflow_immediately(self):
        control, values = self.run_constant(60, 92, duration=120)
        previous = values[-1]
        duty = control.update(self.config, 60, previous, 121, 0)
        self.assertGreater(duty, 20)
        self.assertGreaterEqual(duty, previous - .25)

    def test_emergency_is_immediate_and_cannot_stick_after_recovery(self):
        control = fan.AdaptiveCPU(60, 0)
        self.assertEqual(control.update(self.config, 68, 1, 1, 100), 100)
        duty = 100
        for now in range(2, 402):
            duty = control.update(self.config, 60, duty, now, 92)
        self.assertLess(duty, 30)
        self.assertEqual(control.trim, 0)

    def test_guard_band_and_long_read_do_not_delay_emergency(self):
        control = fan.AdaptiveCPU(65, 0)
        duty = control.update(self.config, 67, 20, 1, 0)
        self.assertEqual(control.state, 'high_temperature')
        self.assertGreater(duty, 20)
        self.assertEqual(control.update(self.config, 68, duty, 100, None), 100)
        duty = control.update(self.config, 60, 100, 200, None)
        self.assertGreaterEqual(duty, 98.75)

    def test_load_unavailable_retains_airflow_and_temperature_feedback(self):
        control, values = self.run_constant(60, 92, duration=120)
        duty = control.update(self.config, 60, values[-1], 121, None)
        self.assertFalse(control.load_available)
        self.assertGreater(duty, 20)
        self.assertEqual(control.update(self.config, 68, duty, 122, None), 100)

    def test_config_edit_discards_old_trim_and_forecast(self):
        control, values = self.run_constant(62, 92, duration=120)
        self.assertGreater(control.trim, 0)
        self.config['cpu_target_c'] = 59
        control.update(self.config, 58, values[-1], 121, 0)
        self.assertEqual(control.trim, 0)
        self.assertEqual(control.filtered, 58)

    def test_invalid_predictive_options_are_rejected(self):
        for options in [{'unknown': 1}, {'trim_interval_seconds': 0},
                        {'fall_percent_per_second': float('nan')}, {'deadband_c': True},
                        {'load_curve': [[0, 1], [50, 10]]},
                        {'load_curve': [[0, 1], [50, 20], [100, 10]]},
                        {'load_curve': [[0, 1], [100, float('inf')]]}]:
            with self.subTest(options=options):
                config = copy.deepcopy(self.config)
                config['predictive'] = options
                with self.assertRaises(ValueError):
                    fan.validate_config(config)

    def test_delayed_thermal_model_settles_under_sustained_load_and_releases(self):
        # A qualitative regression model, not a calibrated Quanta heat model:
        # 8-second fan lag, 45-second thermal lag, nonlinear minimum airflow.
        control = fan.AdaptiveCPU(40, 0)
        temperature, airflow, duty = 40, 1, 1
        history = []
        for now in range(1, 1501):
            load = 92 if now <= 900 else 0
            duty = control.update(self.config, temperature, duty, now, load)
            actual = math.ceil(duty * 63 / 100) * 100 / 63
            airflow += (actual - airflow) / 8
            equilibrium = 40 + .30 * load - .4 * max(0, airflow - 10)
            temperature += (equilibrium - temperature) / 45
            history.append((temperature, duty))
        stable = history[600:900]
        self.assertLess(max(t for t, _ in stable) - min(t for t, _ in stable), 2)
        self.assertTrue(all(58 <= t <= 62 for t, _ in stable))
        self.assertLess(max(d for _, d in stable) - min(d for _, d in stable), 8)
        self.assertGreater(min(d for _, d in stable), 15)
        self.assertLess(max(d for _, d in history), 50)
        self.assertLess(history[-1][1], 5)


if __name__ == '__main__':
    unittest.main()
