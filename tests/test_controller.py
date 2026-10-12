import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('controller', ROOT / 'quanta-fan-control.py')
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)
IDENTITY = '''Firmware Revision         : 7.54
Manufacturer ID           : 7244
Product ID                : 12882
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / 'config/quanta-fan-control.json').read_text())
        self.config = controller.validate_config(self.raw)
        self.fans = set(self.config['required_fans'])
        self.temps = {'CPU0 Temp', 'CPU1 Temp', 'PCH Temp', 'Ambient_Temp',
                      'BP_Temp', 'Inlet_Temp', 'Exhaust_Temp', 'Temp_DIMM_A0'}
        self.sensors = {name: {'value': 40, 'unit': 'degrees C', 'status': 'ok'} for name in self.temps}
        self.sensors.update({name: {'value': 4000, 'unit': 'RPM', 'status': 'ok'} for name in self.fans})

    def demand(self, core=55, critical=100):
        return controller.demand(self.config, self.sensors, core, critical, self.fans, self.temps)

    def test_exported_curve_is_valid_and_inheritance_does_not_edit_input(self):
        before = copy.deepcopy(self.raw)
        for name in controller.CURVE_KEYS:
            self.assertEqual(self.config[name], self.raw['cpu_curve'])
            self.assertEqual(self.config['curve_sources'][name], 'cpu_curve')
        self.assertEqual(self.raw, before)

    def test_invalid_curve_cannot_remove_full_speed_endpoint(self):
        for replacement in [20, 99]:
            with self.subTest(replacement=replacement):
                config = copy.deepcopy(self.raw)
                config['cpu_curve'][-1][1] = replacement
                with self.assertRaisesRegex(ValueError, 'final point must be 100%'):
                    controller.validate_config(config)

    def test_full_speed_cpu_threshold_cannot_exceed_68(self):
        self.raw['cpu_curve'][-1][0] = 69
        with self.assertRaisesRegex(ValueError, '<=68 C'):
            controller.validate_config(self.raw)

    def test_malformed_and_nonfinite_curves_are_rejected(self):
        for points in [[], [[0, 5]], [[0, 5], [0, 100]], [[0, 20], [60, 10], [65, 100]],
                       [[0, 4], [65, 100]], [[0, 5], [float('nan'), 100]], [[False, 5], [65, 100]]]:
            with self.subTest(points=points):
                config = copy.deepcopy(self.raw)
                config['cpu_curve'] = points
                with self.assertRaises(ValueError):
                    controller.validate_config(config)

    def test_step_threshold_boundaries(self):
        for temperature, expected in [(0, 5), (49.9, 5), (50, 10), (59.9, 10), (60, 20), (64.9, 20), (65, 100), (80, 100)]:
            with self.subTest(temperature=temperature):
                self.assertEqual(controller.interpolate(self.config['cpu_curve'], temperature, 'step'), expected)

    def test_linear_interpolation(self):
        self.assertEqual(controller.interpolate([[40, 10], [60, 100]], 50), 55)

    def test_cpu_demand_uses_hottest_core_or_bmc_cpu(self):
        self.sensors['CPU1 Temp']['value'] = 65
        self.assertEqual(self.demand(core=50)[0], 100)

    def test_cpu_only_still_checks_other_temperature_alarms(self):
        self.sensors['PCH Temp']['status'] = 'cr'
        target, faults, _ = self.demand()
        self.assertEqual(target, 100)
        self.assertIn('temperature alarm: PCH Temp', faults)

    def test_missing_and_invalid_sensors_request_full_speed(self):
        for value in [None, float('nan'), -1, 121]:
            with self.subTest(value=value):
                self.sensors['CPU0 Temp']['value'] = value
                target, faults, _ = self.demand()
                self.assertEqual(target, 100)
                self.assertTrue(faults)

    def test_failed_fan_requests_full_speed(self):
        self.sensors['SYS_FAN1-1']['value'] = 0
        target, faults, _ = self.demand()
        self.assertEqual(target, 100)
        self.assertIn('fan failure: SYS_FAN1-1', faults)

    def test_hardware_critical_margin_requests_full_speed(self):
        self.assertEqual(self.demand(core=90, critical=100)[0], 100)
        self.assertIn('CPU near hardware limit', self.demand(core=90, critical=100)[1])

    def test_all_sensor_mode_uses_highest_curve_demand(self):
        self.config['cpu_only'] = False
        self.sensors['PCH Temp']['value'] = 65
        self.assertEqual(self.demand()[0], 100)

    def test_pipe_and_csv_sensor_formats_and_missing_readings(self):
        for text in ['CPU0 Temp | 50 | degrees C | ok\nSYS_FAN1-1 | na | RPM | ns',
                     'CPU0 Temp,50,degrees C,ok\nSYS_FAN1-1,na,RPM,ns']:
            with self.subTest(text=text):
                parsed = controller.parse_sensors(text)
                self.assertEqual(parsed['CPU0 Temp']['value'], 50)
                self.assertIsNone(parsed['SYS_FAN1-1']['value'])

    def test_check_rejects_unverified_bmc_before_sensor_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(self.raw))
            with patch.object(controller, 'CONFIG', str(config)), patch('sys.argv', ['controller', '--check']), \
                 patch.object(controller, 'command', return_value='Firmware Revision : 9.99') as command, \
                 patch.object(controller, 'read_sensors') as sensors:
                with self.assertRaisesRegex(RuntimeError, 'restricted to verified'):
                    controller.main()
                command.assert_called_once_with('mc', 'info')
                sensors.assert_not_called()

    def test_check_reports_faults_without_writing_fan_control(self):
        self.sensors['SYS_FAN1-1']['value'] = 0
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(self.raw))
            with patch.object(controller, 'CONFIG', str(config)), patch('sys.argv', ['controller', '--check']), \
                 patch.object(controller, 'command', return_value=IDENTITY) as command, \
                 patch.object(controller, 'read_sensors', return_value=self.sensors), \
                 patch.object(controller, 'core_temperatures', return_value=(55, 100)), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                controller.main()
                self.assertTrue(json.loads(output.getvalue())['faults'])
                command.assert_called_once_with('mc', 'info')

    def test_bmc_duty_rounds_up_and_reasserts_manual_mode(self):
        with patch.object(controller, 'command') as command:
            self.assertEqual(controller.manual(5), 6.3)
            self.assertEqual(command.call_args_list[0].args, ('raw', '0x30', '0xf9', '0xff', '0x00'))
            self.assertEqual(command.call_args_list[1].args, ('raw', '0x30', '0xf9', '0x00', '0x4'))

    def test_exception_after_entering_manual_mode_restores_automatic(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(controller, 'read_config', return_value=self.config), \
                 patch.object(controller, 'command', return_value=IDENTITY), \
                 patch.object(controller, 'read_sensors', return_value=self.sensors), \
                 patch.object(controller, 'core_temperatures', return_value=(55, 100)), \
                 patch.object(controller, 'STATE', str(Path(directory) / 'status.json')), \
                 patch.object(controller, 'manual', side_effect=RuntimeError('write failed')), \
                 patch.object(controller, 'automatic') as automatic, \
                 patch.object(controller, 'notify'), patch('signal.signal'), \
                 patch('sys.argv', ['controller']), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'write failed'):
                    controller.main()
                automatic.assert_called_once()


if __name__ == '__main__':
    unittest.main()
