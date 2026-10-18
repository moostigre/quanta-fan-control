#!/usr/bin/python3
"""Replay recorded temperatures/load without accessing hardware.

Input CSV: timestamp,temperature_c,cpu_load_percent (blank load is unavailable).
Timestamps are increasing seconds; temperatures must be the configured CPU
control input (hottest or average). This single-input replay does not model
the independent peak-temperature safety overrides used by average mode.
This checks decisions against fixed observations, not the counterfactual heat
response that different fan commands would have produced.
"""
import argparse
import csv
import importlib.util
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def replay(config, samples, controller):
    control, duty, previous_time = None, None, None
    for sample in samples:
        now, temperature = float(sample['timestamp']), float(sample['temperature_c'])
        raw_load = sample.get('cpu_load_percent', '')
        load = float(raw_load) if raw_load.strip() else None
        if (not math.isfinite(now) or not math.isfinite(temperature)
                or not 0 <= temperature <= 120
                or (previous_time is not None and now <= previous_time)
                or (load is not None and (not math.isfinite(load) or not 0 <= load <= 100))):
            raise ValueError('Invalid temperature/load or non-increasing timestamp')
        if control is None:
            control = controller.AdaptiveCPU(temperature, now)
            duty = controller.interpolate(config['cpu_curve'], temperature,
                                          config.get('curve_mode', 'linear'))
        else:
            duty = control.update(config, temperature, duty, now, load)
        previous_time = now
        yield {'timestamp': now, 'temperature_c': temperature,
               'cpu_load_percent': load, 'target_percent': round(duty, 3),
               'effective_percent': round(math.ceil(duty * 63 / 100) * 100 / 63, 3),
               **control.status()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('fan', ROOT / 'quanta-fan-control.py')
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    config = controller.validate_config(json.loads(args.config.read_text()))
    with args.input.open(newline='') as source:
        writer = None
        for result in replay(config, csv.DictReader(source), controller):
            if writer is None:
                writer = csv.DictWriter(sys.stdout, fieldnames=list(result))
                writer.writeheader()
            writer.writerow(result)


if __name__ == '__main__':
    main()
