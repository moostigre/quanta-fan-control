#!/usr/bin/python3
"""Run the controller's read-only check against a selected configuration."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'quanta-fan-control.py'
    spec = importlib.util.spec_from_file_location('quanta_fan_control', source)
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    controller.CONFIG = str(args.config.resolve())
    sys.argv = [str(source), '--check']
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        controller.main()
    result = json.loads(output.getvalue())
    print(json.dumps(result, indent=2))
    if result['faults']:
        print('Preflight failed: ' + '; '.join(result['faults']), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
