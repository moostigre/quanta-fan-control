#!/usr/bin/python3
"""Quanta S210-X12RS firmware 7.54 fan controller. No firmware writes.
Config points are [temperature C, duty percent]. Highest thermal demand wins.
Empty lists inherit the first non-empty curve in CURVE_KEYS order (CPU first).
With cpu_only=true only CPU temperature determines normal duty.
With curve_mode=step each speed is held until the next temperature threshold.
On normal exit and systemd watchdog failure, return to native BMC control.
A host-wide freeze cannot be covered by a userspace/systemd watchdog.
"""
import argparse, csv, glob, io, json, math, os, pathlib, signal, socket, subprocess, time
CONFIG = '/etc/quanta-fan-control.json'
IPMI = '/usr/bin/ipmitool'
STATE = '/run/quanta-fan-control/status.json'
SDR_CACHE = '/var/lib/quanta-fan-control/sdr-cache.bin'
stop = False

def notify(message):
    address = os.getenv('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'): address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address); sock.sendall(message.encode())

def command(*args):
    result = subprocess.run([IPMI, *args], text=True, capture_output=True, timeout=8)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'IPMI command failed')
    return result.stdout

def automatic():
    command('raw', '0x30', '0xf9', '0xff', '0x01')

def manual(percent):
    raw = min(63, max(1, math.ceil(percent * 63 / 100)))
    # Reassert each cycle so that a BMC restart does not silently lose the curve.
    command('raw', '0x30', '0xf9', '0xff', '0x00')
    command('raw', '0x30', '0xf9', '0x00', hex(raw))
    return round(raw * 100 / 63, 1)

CURVE_KEYS = ('cpu_curve', 'dimm_curve', 'pch_curve', 'ambient_curve',
              'backplane_curve', 'inlet_curve', 'exhaust_curve')
MINIMUM_DUTY_PERCENT = 1

def validate_config(c):
    """Resolve empty curves in memory; keep the editable file unchanged."""
    if not isinstance(c, dict): raise ValueError('Configuration must be an object')
    resolved = dict(c)
    first = None
    first_key = None
    for key in CURVE_KEYS:
        points = c.get(key)
        if not isinstance(points, list): raise ValueError(key + ': use a point list or [] to inherit')
        if not points: continue
        if len(points) < 2: raise ValueError(key + ': need at least two points')
        previous = (-math.inf, -math.inf)
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(key + ': each point must be [temperature, duty]')
            temperature, duty = point
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) for v in point):
                raise ValueError(key + ': temperatures and duties must be finite numbers')
            if not (temperature > previous[0] and MINIMUM_DUTY_PERCENT <= duty <= 100
                    and duty >= previous[1]):
                raise ValueError(key + ': increasing temperatures and 1–100% duties required')
            previous = (temperature, duty)
        if points[-1][1] != 100: raise ValueError(key + ': final point must be 100%')
        if first is None: first, first_key = points, key
    if first is None: raise ValueError('At least one curve must be non-empty')
    for key in CURVE_KEYS:
        resolved[key] = [list(point) for point in (c[key] or first)]
    resolved['curve_sources'] = {key: key if c[key] else first_key for key in CURVE_KEYS}
    if resolved['cpu_curve'][-1][0] > 68:
        raise ValueError('CPU full-speed point must be <=68 C, including inherited curves')
    if not isinstance(resolved.get('cpu_only', False), bool):
        raise ValueError('cpu_only must be true or false')
    if resolved.get('curve_mode', 'linear') not in ('linear', 'step'):
        raise ValueError('curve_mode must be linear or step')
    if not resolved['required_fans']: raise ValueError('Required fans must be specified')
    if not 1 <= resolved['interval_seconds'] <= 5: raise ValueError('Interval must be 1–5 seconds')
    if not 1 <= resolved['fall_percent_per_cycle'] <= 5: raise ValueError('Fall limit must be 1–5%')
    if not 100 <= resolved['minimum_rpm'] <= 5000: raise ValueError('Minimum RPM must be 100–5000')
    return resolved

def read_config():
    return validate_config(json.loads(pathlib.Path(CONFIG).read_text()))

def interpolate(points, temperature, mode='linear'):
    if mode == 'step':
        for threshold, duty in reversed(points):
            if temperature >= threshold: return duty
        return points[0][1]
    for (a, low), (b, high) in zip(points, points[1:]):
        if temperature <= a: return low
        if temperature <= b: return low + (high-low) * (temperature-a) / (b-a)
    return points[-1][1]

def parse_sensors(text):
    result = {}
    rows = (line.split('|') for line in text.splitlines()) if '|' in text else csv.reader(io.StringIO(text))
    for row in rows:
        fields = [f.strip() for f in row]
        if len(fields) < 4: continue
        try: value = float(fields[1])
        except ValueError: value = None
        result[fields[0]] = {'value': value, 'unit': fields[2], 'status': fields[3]}
    return result

def read_sensors():
    # Cache the inventory only; sdr list still obtains fresh sensor readings.
    # Unlike sensor, it does not re-read thresholds for every row each cycle.
    args = ['-c']
    if pathlib.Path(SDR_CACHE).is_file(): args.extend(['-S', SDR_CACHE])
    return parse_sensors(command(*args, 'sdr', 'list', 'full'))

def core_temperatures():
    values, critical, devices = [], [], 0
    for name in glob.glob('/sys/class/hwmon/hwmon*/name'):
        if pathlib.Path(name).read_text().strip() != 'coretemp': continue
        devices += 1
        for p in pathlib.Path(name).parent.glob('temp*_input'):
            values.append(float(p.read_text()) / 1000)
        for p in pathlib.Path(name).parent.glob('temp*_crit'):
            critical.append(float(p.read_text()) / 1000)
    if devices != 2 or not values or not critical:
        raise RuntimeError('Both CPU coretemp devices are required')
    return max(values), min(critical)

def demand(config, sensors, core_max, core_critical, tracked_fans, tracked_temps):
    reasons, temps = [], {}
    for name in tracked_temps:
        s = sensors.get(name, {})
        value = s.get('value')
        if value is None or not math.isfinite(value) or value < 0 or value > 120:
            reasons.append('missing/invalid temperature: ' + name)
        else:
            temps[name] = value
            if s.get('status') != 'ok': reasons.append('temperature alarm: ' + name)
    for name in tracked_fans:
        s = sensors.get(name, {})
        rpm = s.get('value')
        if rpm is None or not math.isfinite(rpm) or rpm < config['minimum_rpm'] or s.get('status') != 'ok':
            reasons.append('fan failure: ' + name)
    if core_max >= core_critical - 10: reasons.append('CPU near hardware limit')
    if reasons: return 100, reasons, temps
    cpu = max(core_max, temps['CPU0 Temp'], temps['CPU1 Temp'])
    mode = config.get('curve_mode', 'linear')
    cpu_target = interpolate(config['cpu_curve'], cpu, mode)
    if config.get('cpu_only', False): return cpu_target, [], temps
    targets = [cpu_target]
    mapping = {'PCH Temp':'pch_curve', 'Ambient_Temp':'ambient_curve',
               'BP_Temp':'backplane_curve', 'Inlet_Temp':'inlet_curve',
               'Exhaust_Temp':'exhaust_curve'}
    targets.extend(interpolate(config[key], temps[name], mode) for name, key in mapping.items())
    targets.extend(interpolate(config['dimm_curve'], v, mode) for name, v in temps.items() if name.startswith('Temp_DIMM_'))
    return max(targets), [], temps

def on_signal(signum, frame):
    global stop
    stop = True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='validate and report without changing fan control')
    parser.add_argument('--automatic', action='store_true', help='restore native BMC automatic control')
    args = parser.parse_args()
    if args.automatic: automatic(); return
    config = read_config()
    identity = command('mc', 'info')
    if not all(s in identity for s in ['Firmware Revision         : 7.54',
                                     'Manufacturer ID           : 7244',
                                     'Product ID                : 12882']):
        raise RuntimeError('Controller is restricted to verified S210 firmware 7.54')
    sensors = read_sensors()
    tracked_fans = set(config['required_fans']) | {n for n,s in sensors.items() if s['unit']=='RPM' and n.startswith('SYS_FAN') and (s['value'] or 0)>0}
    tracked_temps = {'CPU0 Temp','CPU1 Temp','PCH Temp','Ambient_Temp','BP_Temp','Inlet_Temp','Exhaust_Temp'} | {n for n,s in sensors.items() if n.startswith('Temp_DIMM_') and s['value'] is not None}
    core_max, core_critical = core_temperatures()
    target, reasons, temperatures = demand(config,sensors,core_max,core_critical,tracked_fans,tracked_temps)
    if args.check:
        print(json.dumps({'target_percent':round(target,1),'faults':reasons,'core_max_c':core_max,'temperatures_c':temperatures,'monitored_fans':sorted(tracked_fans),'curve_sources':config['curve_sources'],'cpu_only':config.get('cpu_only', False),'curve_mode':config.get('curve_mode','linear')},indent=2)); return
    if reasons: raise RuntimeError('Initial health check failed: ' + '; '.join(reasons))
    signal.signal(signal.SIGTERM,on_signal); signal.signal(signal.SIGINT,on_signal)
    pathlib.Path(STATE).parent.mkdir(parents=True,exist_ok=True)
    previous, last_log, last_event = target, 0, None
    try:
        notify('READY=1')
        while not stop:
            config = read_config() # edits take effect without a restart
            tracked_fans.update(config['required_fans'])
            sensors = read_sensors()
            # Remember any additional fans/DIMMs reconnected during this run.
            tracked_fans.update(n for n,s in sensors.items() if n.startswith('SYS_FAN') and (s['value'] or 0)>0)
            tracked_temps.update(n for n,s in sensors.items() if n.startswith('Temp_DIMM_') and s['value'] is not None)
            core_max, core_critical = core_temperatures()
            target,reasons,temperatures = demand(config,sensors,core_max,core_critical,tracked_fans,tracked_temps)
            # Never delay rising speed; let falling speed settle gradually.
            target = max(target, previous-config['fall_percent_per_cycle'])
            effective = manual(target); previous = effective
            status = {'timestamp':time.time(),'duty_percent':effective,'target_percent':round(target,1),
                      'cpu_core_max_c':core_max,'temperatures_c':temperatures,'faults':reasons,
                      'rpm':{n:sensors.get(n,{}).get('value') for n in sorted(tracked_fans)},
                      'curve_sources':config['curve_sources'],
                      'cpu_only':config.get('cpu_only', False),
                      'curve_mode':config.get('curve_mode','linear')}
            temporary = pathlib.Path(STATE + '.tmp'); temporary.write_text(json.dumps(status,indent=2)); temporary.replace(STATE)
            event = (round(effective),tuple(reasons))
            if event != last_event or time.monotonic()-last_log>60:
                print('duty=%s%% cpu=%sC faults=%s' % (effective,max(core_max,temperatures.get('CPU0 Temp',0),temperatures.get('CPU1 Temp',0)),reasons),flush=True)
                last_log = time.monotonic(); last_event = event
            notify('WATCHDOG=1\nSTATUS=Fan duty %s%%; CPU %s C' % (effective,core_max))
            end = time.monotonic()+config['interval_seconds']
            while not stop and time.monotonic()<end: time.sleep(.2)
    except Exception:
        if not stop:
            raise
    finally:
        automatic()
        print('Restored native BMC automatic fan control',flush=True)

if __name__ == '__main__': main()
