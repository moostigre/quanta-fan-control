import importlib.util
import json
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_predictive import configuration, fan

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cli', ROOT / 'scripts/fan-control.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CommandTests(unittest.TestCase):
    def test_status_waits_for_first_reading_after_socket_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'control.sock').touch()
            def first_reading():
                threading.Event().wait(.02)
                (runtime / 'status.json').write_text('{"control_mode":"auto"}')
            worker = threading.Thread(target=first_reading)
            worker.start()
            self.assertEqual(cli.read_status(runtime, 1), {'control_mode': 'auto'})
            worker.join()

    def test_examples_and_individual_release(self):
        self.assertEqual(cli.parse_command(['set', 'fan1', '100%']),
                         {'action': 'set', 'fans': [1], 'percent': 100})
        self.assertEqual(cli.parse_command(['set', 'fans', '33.3%'])['fans'], list(range(1, 7)))
        self.assertEqual(cli.parse_command(['set', 'auto', 'on']), {'action': 'auto'})
        self.assertEqual(cli.parse_command(['set', 'fan3', 'auto']), {'action': 'auto', 'fans': [3]})

    def test_bad_commands_and_percentages_cannot_change_hardware(self):
        for args in [[], ['set', 'fan0', '100%'], ['set', 'fan7', '10'],
                     ['set', 'fan1', '0%'], ['set', 'fans', '101%'],
                     ['set', 'fans', 'nan'], ['set', 'fans', 'inf'],
                     ['set', 'auto', 'off'], ['set', 'fans', '20%', 'extra']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                cli.parse_command(args)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.server = fan.ControlServer(self.path / 'control.sock')
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.client.bind(str(self.path / 'reply'))
        self.client.settimeout(1)

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.directory.cleanup()

    def request(self, action='set', **fields):
        request = dict(id='test-request', action=action, **fields)
        self.client.sendto(json.dumps(request).encode(), str(self.path / 'control.sock'))
        return self.server.poll()

    def test_socket_is_root_private_and_single_override_preserves_other_fans(self):
        self.assertEqual(stat.S_IMODE(self.server.path.stat().st_mode), 0o600)
        pending, force = self.request(fans=[1], percent=100)
        self.assertEqual(force, {1})
        self.assertEqual(self.server.overrides, {1: 100})
        self.request(fans=[3], percent=50)
        self.assertEqual(self.server.overrides, {1: 100, 3: 50})
        self.assertEqual(len(pending), 1)

    def test_resume_auto_clears_all_overrides_and_can_release_one_fan(self):
        self.request(fans=list(range(1, 7)), percent=100)
        _, force = self.request('auto', fans=[3])
        self.assertEqual(force, {3})
        self.assertNotIn(3, self.server.overrides)
        _, force = self.request('auto')
        self.assertEqual(force, {1, 2, 4, 5, 6})
        self.assertEqual(self.server.overrides, {})

    def test_invalid_request_is_rejected_without_overwriting_existing_override(self):
        self.request(fans=[1], percent=50)
        for fields in [{'fans': [1], 'percent': float('nan')},
                       {'fans': [True], 'percent': 100}, {'fans': [7], 'percent': 10},
                       {'fans': [1, 1], 'percent': 10}, {'fans': [1], 'percent': False}]:
            pending, force = self.request(**fields)
            self.assertEqual((pending, force), ([], set()))
            self.assertFalse(json.loads(self.client.recv(8192))['ok'])
            self.assertEqual(self.server.overrides, {1: 50})

    def test_acknowledgement_waits_for_hardware_stage_and_reports_safety(self):
        pending, _ = self.request(fans=[1], percent=1)
        self.client.setblocking(False)
        with self.assertRaises(BlockingIOError): self.client.recv(8192)
        self.server.acknowledge(pending, {'manual_overrides': {'1': 1},
            'fan_duty_percent': {'1': 100}, 'manual_safety_override': True,
            'faults': ['fan failure: SYS_FAN2-1']})
        self.client.settimeout(1)
        result = json.loads(self.client.recv(8192))
        self.assertTrue(result['ok'])
        self.assertTrue(result['safety_override'])
        self.assertEqual(result['fan_duty_percent']['1'], 100)

    def test_real_client_daemon_round_trip(self):
        done = threading.Event()
        results = []
        def send():
            try: results.append(cli.send_request({'action': 'set', 'fans': [1], 'percent': 100}, self.path, 2))
            except Exception as error: results.append(error)
            finally: done.set()
        worker = threading.Thread(target=send)
        worker.start()
        for _ in range(100):
            pending, _ = self.server.poll()
            if pending:
                self.server.acknowledge(pending, {'manual_overrides': {'1': 100},
                    'fan_duty_percent': {'1': 100}, 'manual_safety_override': False, 'faults': []})
                break
            done.wait(.01)
        worker.join(3)
        self.assertTrue(done.is_set())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], dict)
        self.assertTrue(results[0]['ok'])


class ManualOutputTests(unittest.TestCase):
    def test_manual_single_fan_is_immediate_and_other_fans_remain_automatic(self):
        config = configuration()
        config['fan_staging'] = {'enabled': True}
        output = fan.StagedFans()
        with patch.object(fan, 'command'):
            output.apply(config, 20, 0)
            output.apply(config, 20, .1, overrides={1: 100}, force={1})
            self.assertEqual(output.raw[1], 63)
            self.assertEqual(output.raw[2], 1)
            self.assertEqual(output.raw[3], 13)
            output.apply(config, 20, .2, overrides={}, force={1})
            self.assertEqual(output.raw[1], 1)

    def test_manual_override_also_works_with_uniform_auto_configuration(self):
        output = fan.StagedFans()
        with patch.object(fan, 'command'):
            output.apply(configuration(), 20, 0, overrides={1: 100}, force={1})
            self.assertEqual(output.raw[1], 63)
            self.assertEqual({v for i, v in output.raw.items() if i != 1}, {13})

    def test_manual_minimum_cannot_bypass_emergency_or_urgent_heat(self):
        output = fan.StagedFans()
        with patch.object(fan, 'command'):
            output.apply(configuration(), 50, 0, urgent=True, overrides={1: 1}, force={1})
            self.assertEqual(output.raw[1], 32)
            output.apply(configuration(), 100, 1, emergency=True, overrides={1: 1}, force={1})
            self.assertEqual(set(output.raw.values()), {63})

    def test_manual_airflow_cannot_teach_an_invalid_cooling_correction(self):
        control = fan.AdaptiveCPU(60, 0)
        duty = 26
        for now in range(1, 181):
            duty = control.update(configuration(), 50, duty, now, 92, learn=False)
        self.assertEqual(control.trim, 0)
        self.assertIsNone(control.cool_since)


if __name__ == '__main__':
    unittest.main()
