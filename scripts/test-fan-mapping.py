#!/usr/bin/python3
"""Verify fan-1 mapping using increased airflow only; never request zero duty."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

SERVICE = 'quanta-fan-control.service'
REPORT = pathlib.Path('/root/quanta-firmware-research/fan1-mapping-test.json')


def restore():
    subprocess.run(['/usr/bin/ipmitool', 'raw', '0x30', '0xf9', '0xff', '0x01'],
                   check=True, timeout=8, capture_output=True)
    subprocess.run(['systemctl', 'start', SERVICE], check=True, timeout=40)


def main():
    if sys.argv[1:] == ['--restore']:
        restore()
        return
    if sys.argv[1:]:
        raise SystemExit('Usage: test-fan-mapping.py [--restore]')
    source = pathlib.Path('/usr/local/sbin/quanta-fan-control.py')
    spec = importlib.util.spec_from_file_location('controller', source)
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    samples = []

    def sample(phase):
        sensors = controller.read_sensors()
        core, critical = controller.core_temperatures()
        fans = {n: s['value'] for n, s in sensors.items() if n.startswith('SYS_FAN')}
        temps = {n: s['value'] for n, s in sensors.items() if s['unit'] == 'degrees C' and s['value'] is not None}
        samples.append({'time': time.time(), 'phase': phase, 'core_max_c': core,
                        'temperatures_c': temps, 'rpm': fans})
        REPORT.write_text(json.dumps(samples, indent=2) + '\n')
        print(json.dumps(samples[-1]), flush=True)
        if (len(fans) != 12 or any(v is None or v < 1000 for v in fans.values())
                or any(s['status'] != 'ok' for n, s in sensors.items()
                       if n in fans or n in temps)
                or max(core, temps.get('CPU0 Temp', 120), temps.get('CPU1 Temp', 120)) >= 55
                or core >= critical - 15):
            raise RuntimeError('Test health guard failed; restoring normal control')

    subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE], check=True)
    sample('before')
    subprocess.run(['systemd-run', '--unit=quanta-fan-mapping-restore', '--on-active=90s',
                    '/usr/bin/python3', str(pathlib.Path(__file__).resolve()), '--restore'], check=True)
    try:
        subprocess.run(['systemctl', 'stop', SERVICE], check=True, timeout=30)
        controller.manual(1)
        for _ in range(4):
            time.sleep(5)
            sample('all-raw-1')
        controller.command('raw', '0x30', '0xf9', '0x01', '0x10')
        for _ in range(4):
            time.sleep(5)
            sample('selector-1-raw-16')
    finally:
        restore()
        subprocess.run(['systemctl', 'stop', 'quanta-fan-mapping-restore.timer'], check=True)
    print('Normal fan controller restored', flush=True)


if __name__ == '__main__':
    main()
