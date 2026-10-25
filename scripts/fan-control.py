#!/usr/bin/python3
"""Send manual fan commands to the supervised Quanta fan controller."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

RUNTIME = Path('/run/quanta-fan-control')

def read_status(runtime=RUNTIME, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return json.loads((runtime / 'status.json').read_text())
        except FileNotFoundError:
            # systemd READY and socket creation precede the first sensor cycle.
            if not (runtime / 'control.sock').exists() or time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def parse_command(args):
    if args == ['set', 'auto', 'on']:
        return {'action': 'auto'}
    if len(args) != 3 or args[0] != 'set':
        raise ValueError('Use: set fan1 100%, set fans 100%, or set auto on')
    target, value = args[1:]
    match = re.fullmatch(r'fan([1-6])', target)
    if target == 'fans': selectors = list(range(1, 7))
    elif match: selectors = [int(match[1])]
    else: raise ValueError('Choose fan1 through fan6, or fans')
    if value == 'auto':
        return {'action': 'auto', 'fans': selectors}
    percent = float(value.removesuffix('%'))
    if not math.isfinite(percent) or not 1 <= percent <= 100:
        raise ValueError('Fan duty must be 1-100%')
    return {'action': 'set', 'fans': selectors, 'percent': percent}


def send_request(request, runtime=RUNTIME, timeout=25):
    # Serialize interactive commands so each acknowledgement corresponds to
    # one applied change rather than several concurrent, conflicting requests.
    with (runtime / 'client.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='cli-', dir=runtime) as directory:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                path = str(Path(directory) / 'reply')
                client.bind(path)
                os.chmod(path, 0o600)
                request = dict(request, id=uuid.uuid4().hex)
                client.sendto(json.dumps(request).encode(), str(runtime / 'control.sock'))
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise TimeoutError('Controller acknowledgement timed out')
                    client.settimeout(remaining)
                    data, address = client.recvfrom(8192)
                    if address != str(runtime / 'control.sock'): continue
                    reply = json.loads(data)
                    if reply.get('id') != request['id']: continue
                    if not reply.get('ok'):
                        raise ValueError(reply.get('error', 'Control request rejected'))
                    return reply


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        epilog='Examples: fan-control set fan1 100%; fan-control set fans 100%; fan-control set auto on')
    parser.add_argument('command', nargs='*')
    args = parser.parse_args().command
    try:
        if args == ['status']:
            state = read_status()
            print(json.dumps({key: state.get(key) for key in
                ('control_mode', 'fan_control_mode', 'cpu_max_c', 'cpu_average_c', 'cpu_control_c',
                 'cpu_temperature_mode', 'cpu_target_c',
                 'cpu_load_percent', 'fan_stages_active', 'manual_overrides', 'fan_duty_percent', 'rpm',
                 'cpu_sockets', 'fan_target_rpm', 'fan_rpm_sensor',
                 'manual_safety_override', 'faults')}, indent=2))
            return 0
        request = parse_command(args)
        if os.geteuid() != 0: raise ValueError('Run fan control commands as root or with sudo')
        if request['action'] == 'auto':
            subprocess.run(['systemctl', 'start', 'quanta-fan-control.service'],
                           check=True, timeout=40, capture_output=True, text=True)
        reply = send_request(request)
        overrides = reply['manual_overrides']
        print('Manual overrides: ' + (', '.join('fan%s=%g%%' % (i, v)
              for i, v in sorted(overrides.items())) if overrides else 'none (automatic control)'))
        print('Effective duty: ' + ', '.join('fan%s=%g%%' % (i, v)
              for i, v in sorted(reply['fan_duty_percent'].items())))
        if reply['safety_override']:
            print('Thermal/fault protection is overriding manual settings: ' + '; '.join(reply['faults']))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Fan control: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
