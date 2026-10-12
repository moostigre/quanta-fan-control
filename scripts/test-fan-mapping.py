#!/usr/bin/python3
"""Guarded fan-1 mapping test; --stop-fan1 explicitly tests zero duty."""
import importlib.util
import json
import os
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
    stop_test = sys.argv[1:] == ['--stop-fan1']
    if sys.argv[1:] and not stop_test:
        raise SystemExit('Usage: test-fan-mapping.py [--restore|--stop-fan1]')
    report = REPORT.with_name('fan1-zero-duty-test.json') if stop_test else REPORT
    report.parent.mkdir(parents=True, exist_ok=True)
    restore_unit = 'quanta-fan-test-restore-' + str(os.getpid())
    source = pathlib.Path('/usr/local/sbin/quanta-fan-control.py')
    spec = importlib.util.spec_from_file_location('controller', source)
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    inspector_spec = importlib.util.spec_from_file_location(
        'inspector', pathlib.Path(__file__).with_name('inspect-fan-controller.py'))
    inspector = importlib.util.module_from_spec(inspector_spec)
    inspector_spec.loader.exec_module(inspector)
    samples = []

    def sample(phase):
        sensors = controller.read_sensors()
        core, critical = controller.core_temperatures()
        fans = {n: s['value'] for n, s in sensors.items() if n.startswith('SYS_FAN')}
        temps = {n: s['value'] for n, s in sensors.items() if s['unit'] == 'degrees C' and s['value'] is not None}
        samples.append({'time': time.time(), 'phase': phase, 'core_max_c': core,
                        'temperatures_c': temps, 'rpm': fans})
        if stop_test:
            bank_before = inspector.read_register(0)
            registers = {hex(r): inspector.read_register(r) for r in (0xb0, 0xb1, 0xb6)}
            bank_after = inspector.read_register(0)
            samples[-1]['controller_registers'] = registers
            samples[-1]['bank_before'] = bank_before
            samples[-1]['bank_after'] = bank_after
            if bank_before & 7 or bank_after != bank_before:
                raise RuntimeError('Controller bank changed; restoring normal control')
        report.write_text(json.dumps(samples, indent=2) + '\n')
        print(json.dumps(samples[-1]), flush=True)
        allowed_stopped = {'SYS_FAN1-1', 'SYS_FAN1-2'} if phase == 'selector-1-raw-0' else set()
        if (len(fans) != 12 or any(v is None or (v < 1000 and n not in allowed_stopped) for n, v in fans.items())
                or any(s['status'] != 'ok' for n, s in sensors.items()
                       if (n in fans or n in temps) and n not in allowed_stopped)
                or max(core, temps.get('CPU0 Temp', 120), temps.get('CPU1 Temp', 120)) >= 55
                or any(temps.get(n, 120) > value + 5
                       for n, value in samples[0]['temperatures_c'].items())
                or core >= critical - 15):
            raise RuntimeError('Test health guard failed; restoring normal control')

    subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE], check=True)
    sample('before')
    subprocess.run(['systemd-run', '--unit=' + restore_unit, '--on-active=90s',
                    '/usr/bin/python3', str(pathlib.Path(__file__).resolve()), '--restore'], check=True)
    try:
        subprocess.run(['systemctl', 'stop', SERVICE], check=True, timeout=30)
        if stop_test:
            controller.manual(16 * 100 / 63)
            for _ in range(2):
                time.sleep(5)
                sample('all-raw-16')
            controller.command('raw', '0x30', '0xf9', '0x01', '0x00')
            for _ in range(6):
                time.sleep(2)
                sample('selector-1-raw-0')
            controller.command('raw', '0x30', '0xf9', '0x01', '0x10')
            for _ in range(3):
                time.sleep(3)
                sample('fan1-restart-raw-16')
        else:
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
        subprocess.run(['systemctl', 'stop', restore_unit + '.timer'], check=True)
    print('Normal fan controller restored', flush=True)


if __name__ == '__main__':
    main()
