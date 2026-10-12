import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preflight', ROOT / 'scripts/preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def check(self, faults):
        controller = SimpleNamespace(main=lambda: print(json.dumps({'faults': faults})))
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=Mock()))
        output, errors = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['preflight', '--config', str(ROOT / 'config/quanta-fan-control.json')]), \
             patch.object(preflight.importlib.util, 'spec_from_file_location', return_value=fake_spec), \
             patch.object(preflight.importlib.util, 'module_from_spec', return_value=controller), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = preflight.main()
        self.assertEqual(controller.CONFIG, str(ROOT / 'config/quanta-fan-control.json'))
        return result, json.loads(output.getvalue()), errors.getvalue()

    def test_healthy_read_only_check_allows_installation(self):
        result, output, errors = self.check([])
        self.assertEqual(result, 0)
        self.assertEqual(output['faults'], [])
        self.assertEqual(errors, '')

    def test_health_fault_blocks_installation_even_if_controller_check_returns_normally(self):
        result, output, errors = self.check(['fan failure: SYS_FAN1-1'])
        self.assertEqual(result, 1)
        self.assertEqual(output['faults'], ['fan failure: SYS_FAN1-1'])
        self.assertIn('Preflight failed', errors)
