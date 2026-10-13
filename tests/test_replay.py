import importlib.util
from pathlib import Path
import unittest

from test_predictive import configuration, fan

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('replay', ROOT / 'scripts/replay-predictive.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class ReplayTests(unittest.TestCase):
    def test_replay_accepts_unavailable_load_and_reports_raw_emergency(self):
        samples = [dict(timestamp='0', temperature_c='60', cpu_load_percent=''),
                   dict(timestamp='1', temperature_c='68', cpu_load_percent='92'),
                   dict(timestamp='2', temperature_c='60', cpu_load_percent='92')]
        rows = list(replay.replay(configuration(), samples, fan))
        self.assertEqual(rows[1]['target_percent'], 100)
        self.assertLess(rows[2]['target_percent'], 100)
        self.assertEqual(rows[1]['cpu_controller_state'], 'emergency')

    def test_replay_rejects_invalid_values_and_reversed_clock(self):
        for temperature, load, timestamp in [('nan', '10', '1'), ('60', '101', '1'),
                                              ('60', '10', '-1')]:
            samples = [dict(timestamp='0', temperature_c='60', cpu_load_percent='10'),
                       dict(timestamp=timestamp, temperature_c=temperature, cpu_load_percent=load)]
            with self.assertRaises(ValueError):
                list(replay.replay(configuration(), samples, fan))
