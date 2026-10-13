#!/usr/bin/python3
"""Quanta S210-X12RS firmware 7.54 fan controller. No firmware writes.
Config points are [temperature C, duty percent]. Highest thermal demand wins.
Empty lists inherit the first non-empty curve in CURVE_KEYS order (CPU first).
With cpu_only=true only CPU inputs determine normal duty; predictive/adaptive
mode also anticipates heating from local CPU utilization.
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
PREDICTIVE_DEFAULTS = {
    'load_curve': [[0, 1], [25, 8], [50, 15], [75, 22], [100, 28]],
    'temperature_filter_seconds': 5,
    'prediction_seconds': 10,
    'prediction_max_c': 3,
    'load_prediction_c': 3,
    'deadband_c': 1,
    'trim_step_percent': 100 / 63,
    'trim_interval_seconds': 15,
    'cooldown_hold_seconds': 30,
    'cooldown_interval_seconds': 20,
    'rise_percent_per_second': 1,
    'fall_percent_per_second': .25,
    'max_trim_percent': 30,
}
STAGING_DEFAULTS = {
    'enabled': False,
    'groups': [[3, 4], [2, 5], [1, 6]],
    'activation_percent': [0, 20, 32],
    'allocation_mode': 'balanced',
    'balance_floor_percent': 20,
    'priority_bias_percent': 2,
    'idle_percent': 1,
    'stage_hysteresis_percent': 3,
    'stage_up_hold_seconds': 10,
    'stage_down_hold_seconds': 30,
    'output_hold_seconds': 5,
    'output_step_raw': 2,
    'output_hysteresis_raw': .25,
    'refresh_seconds': 30,
}

def staging_options(config):
    return dict(STAGING_DEFAULTS, **config.get('fan_staging', {}))

def validate_staging(config):
    supplied = config.get('fan_staging', {})
    if not isinstance(supplied, dict) or set(supplied) - set(STAGING_DEFAULTS):
        raise ValueError('fan_staging must contain supported staging options')
    options = staging_options(config)
    if not isinstance(options['enabled'], bool):
        raise ValueError('fan_staging.enabled must be true or false')
    if options['allocation_mode'] not in ('priority', 'balanced'):
        raise ValueError('fan_staging.allocation_mode must be priority or balanced')
    groups = options['groups']
    if (not isinstance(groups, list) or len(groups) != 3
            or any(not isinstance(g, list) or len(g) != 2 for g in groups)):
        raise ValueError('fan_staging.groups must contain three pairs')
    selectors = [s for g in groups for s in g]
    if (any(not isinstance(s, int) or isinstance(s, bool) for s in selectors)
            or sorted(selectors) != list(range(1, 7))):
        raise ValueError('fan_staging.groups must include selectors 1-6 exactly once')
    levels = options['activation_percent']
    if (not isinstance(levels, list) or len(levels) != 3
            or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                   or not math.isfinite(v) for v in levels)
            or not 0 == levels[0] < levels[1] < levels[2] < 100):
        raise ValueError('fan_staging.activation_percent must start at 0 and increase below 100')
    bounds = {'balance_floor_percent': (1, 30), 'priority_bias_percent': (0, 5),
              'idle_percent': (1, 10), 'stage_hysteresis_percent': (1, 10),
              'stage_up_hold_seconds': (5, 60), 'stage_down_hold_seconds': (15, 120),
              'output_hold_seconds': (2, 15), 'output_step_raw': (1, 3),
              'output_hysteresis_raw': (.1, 1), 'refresh_seconds': (15, 60)}
    for key, (low, high) in bounds.items():
        v = options[key]
        if (not isinstance(v, (int, float)) or isinstance(v, bool)
                or not math.isfinite(v) or not low <= v <= high):
            raise ValueError('fan_staging.' + key + ': expected %s-%s' % (low, high))
    if not isinstance(options['output_step_raw'], int):
        raise ValueError('fan_staging.output_step_raw must be an integer')
    if options['enabled'] and not config.get('cpu_only', False):
        raise ValueError('Staged CPU airflow requires cpu_only=true')

def predictive_options(config):
    return dict(PREDICTIVE_DEFAULTS, **config.get('predictive', {}))

def validate_predictive(config):
    supplied = config.get('predictive', {})
    if not isinstance(supplied, dict) or set(supplied) - set(PREDICTIVE_DEFAULTS):
        raise ValueError('predictive must be an object containing supported options')
    options = predictive_options(config)
    bounds = {
        'temperature_filter_seconds': (1, 15), 'prediction_seconds': (0, 20),
        'prediction_max_c': (0, 5), 'load_prediction_c': (0, 5),
        'deadband_c': (.5, 2), 'trim_step_percent': (.5, 5),
        'trim_interval_seconds': (5, 60), 'cooldown_hold_seconds': (10, 120),
        'cooldown_interval_seconds': (10, 120),
        'rise_percent_per_second': (.1, 5), 'fall_percent_per_second': (.05, 1),
        'max_trim_percent': (5, 50),
    }
    for key, (low, high) in bounds.items():
        value = options[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ValueError('predictive.' + key + ': expected %s-%s' % (low, high))
    points = options['load_curve']
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('predictive.load_curve requires at least two points')
    previous = (-1, 0)
    for point in points:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in point)):
            raise ValueError('predictive.load_curve requires finite [load percent, duty] points')
        load, duty = point
        if not previous[0] < load <= 100 or not previous[1] <= duty <= 100 or duty < 1:
            raise ValueError('predictive.load_curve requires increasing loads and 1-100% duties')
        previous = (load, duty)
    if points[0][0] != 0 or points[-1][0] != 100:
        raise ValueError('predictive.load_curve must cover 0-100% CPU utilization')

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
    if resolved.get('cpu_control_mode', 'curve') not in ('curve', 'adaptive', 'predictive'):
        raise ValueError('cpu_control_mode must be curve, adaptive or predictive')
    validate_predictive(resolved)
    validate_staging(resolved)
    if resolved.get('cpu_control_mode') in ('adaptive', 'predictive'):
        if not resolved.get('cpu_only', False):
            raise ValueError('Predictive/adaptive CPU control requires cpu_only=true')
        target = resolved.get('cpu_target_c', 60)
        if (isinstance(target, bool) or not isinstance(target, (int, float))
                or not math.isfinite(target) or not 40 <= target <= 60
                or target > resolved['cpu_curve'][-1][0] - 3):
            raise ValueError('cpu_target_c must be 40-60 C and at least 3 C below full speed')
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

def cooldown(target, previous, fall):
    return max(target, previous - fall)

def remaining_delay(started, interval):
    return max(0, started + interval - time.monotonic())

def validate_control_request(request):
    if not isinstance(request, dict) or request.get('action') not in ('set', 'auto'):
        raise ValueError('Expected set or auto action')
    selectors = request.get('fans', list(range(1, 7)))
    if (not isinstance(selectors, list) or not selectors
            or any(not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= 6 for i in selectors)
            or len(set(selectors)) != len(selectors)):
        raise ValueError('Fan selectors must be unique integers 1-6')
    result = {'action': request['action'], 'fans': selectors}
    if request['action'] == 'set':
        percent = request.get('percent')
        if (not isinstance(percent, (int, float)) or isinstance(percent, bool)
                or not math.isfinite(percent) or not 1 <= percent <= 100):
            raise ValueError('Manual fan duty must be 1-100%')
        result['percent'] = percent
    return result


class ControlServer:
    """Root-only datagram commands; the daemon remains the sole IPMI writer."""
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.overrides = {}
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        # RuntimeDirectory is removed on stop. A stale socket from an interrupted
        # daemon can be replaced; never replace an unrelated regular file.
        if self.path.exists():
            if not self.path.is_socket():
                self.socket.close()
                raise RuntimeError('Control socket path is not a socket')
            self.path.unlink()
        try:
            self.socket.bind(str(self.path))
            self.path.chmod(0o600)
            self.socket.setblocking(False)
        except Exception:
            self.socket.close()
            raise

    def reply(self, address, result):
        try:
            self.socket.sendto(json.dumps(result).encode(), address)
        except (OSError, TypeError):
            pass  # disconnected clients must not interrupt fan safety/control

    def poll(self):
        pending, force = [], set()
        for _ in range(16):
            try:
                data, address = self.socket.recvfrom(4097)
            except BlockingIOError:
                break
            request_id = None
            try:
                if len(data) > 4096: raise ValueError('Control request is too large')
                request = json.loads(data)
                request_id = request.get('id') if isinstance(request, dict) else None
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
                    raise ValueError('Control request requires an id')
                parsed = validate_control_request(request)
                if parsed['action'] == 'set':
                    for i in parsed['fans']: self.overrides[i] = parsed['percent']
                    force.update(parsed['fans'])
                else:
                    force.update(i for i in parsed['fans'] if i in self.overrides)
                    for i in parsed['fans']: self.overrides.pop(i, None)
                pending.append((request_id, address))
            except (ValueError, TypeError, UnicodeDecodeError) as error:
                self.reply(address, {'id': request_id, 'ok': False, 'error': str(error)})
        return pending, force

    def acknowledge(self, pending, status):
        for request_id, address in pending:
            self.reply(address, {'id': request_id, 'ok': True,
                'manual_overrides': status['manual_overrides'],
                'fan_duty_percent': status['fan_duty_percent'],
                'safety_override': status['manual_safety_override'],
                'faults': status['faults']})

    def close(self):
        self.socket.close()
        self.path.unlink(missing_ok=True)


class StagedFans:
    """Allocate CPU airflow in priority order; hold quantized PWM between writes."""
    def __init__(self):
        self.raw = {}
        self.changed_at = {}
        self.refreshed_at = None
        self.signature = None
        self.active = [True, False, False]
        self.up_since = [None] * 3
        self.down_since = [None] * 3
        self.allocated = {}

    def targets(self, options, demand, now, urgent=False):
        idle = options['idle_percent']
        result = {}
        for i, (group, threshold) in enumerate(zip(options['groups'], options['activation_percent'])):
            if i:
                up = (demand > threshold + options['stage_hysteresis_percent']
                      and self.active[i - 1])
                down = demand < threshold - options['stage_hysteresis_percent']
                self.up_since[i] = (now if self.up_since[i] is None else self.up_since[i]) if up else None
                self.down_since[i] = (now if self.down_since[i] is None else self.down_since[i]) if down else None
                if urgent or (self.up_since[i] is not None
                              and now - self.up_since[i] >= options['stage_up_hold_seconds']):
                    self.active[i] = True
                elif (self.down_since[i] is not None
                      and now - self.down_since[i] >= options['stage_down_hold_seconds']):
                    self.active[i] = False
            if i == 0:
                value = max(idle, demand)
            elif self.active[i]:
                value = idle + max(0, demand - threshold) * (100 - idle) / (100 - threshold)
            else:
                value = idle
            # Near the raw-temperature safety threshold, all modules assist.
            if urgent: value = max(value, demand)
            for selector in group: result[selector] = max(idle, value)
        if options['allocation_mode'] == 'balanced' and not urgent:
            # The observed PWM dead zone is not cooling capacity: splitting
            # 30% into two 15% requests would leave both near minimum RPM.
            # Share only demand ABOVE that floor, keeping a small preference
            # for the CPU-facing pair. This is an initial PWM allocation, not
            # a calibrated airflow/RPM model; temperature feedback still wins.
            baseline = max(idle, min(demand, options['balance_floor_percent']))
            budget = max(0, demand - baseline)
            active = [i for i in range(3) if self.active[i]]
            bias = {i: options['priority_bias_percent'] * (len(active) - 1 - rank)
                    for rank, i in enumerate(active)}
            free = list(active)
            extra = {i: 0 for i in active}
            while free:
                level = (budget - sum(bias[i] for i in free)) / len(free)
                positive = [i for i in free if level + bias[i] > 0]
                if positive == free:
                    extra.update({i: level + bias[i] for i in free})
                    break
                free = positive
            for i in active:
                for selector in options['groups'][i]:
                    result[selector] = min(100, baseline + extra[i])
        return result

    def apply(self, config, demand, now, emergency=False, urgent=False, overrides=None, force=()):
        options = staging_options(config)
        signature = json.dumps(options, sort_keys=True)
        if signature != self.signature:
            # Reset allocation timers, retaining the known physical outputs so
            # configuration edits do not introduce a discontinuous PWM jump.
            self.signature = signature
            self.active = [True, False, False]
            self.up_since = [None] * 3
            self.down_since = [None] * 3
            self.refreshed_at = None
        if emergency:
            manual(100)  # broadcast full speed immediately; no lease or staging
            self.raw = {i: 63 for i in range(1, 7)}
            self.changed_at = {i: now for i in self.raw}
            self.refreshed_at = now
            self.active = [True] * 3
            self.allocated = {i: 100 for i in self.raw}
            return {str(i): 100.0 for i in self.raw}
        if options['enabled']:
            targets = self.targets(options, demand, now, urgent)
        else:
            targets = {i: demand for i in range(1, 7)}
            self.active = [True] * 3
        self.allocated = dict(targets)
        for i, percent in (overrides or {}).items():
            targets[i] = max(targets[i], percent) if urgent else percent
        refresh = self.refreshed_at is None or now - self.refreshed_at >= options['refresh_seconds']
        # Bringing helpers through a long PWM dead zone must not withdraw
        # primary cooling first. Prime only automatic helpers to that floor;
        # subsequent RPM-producing increments retain normal step/hold limits.
        prime = set()
        if options['enabled'] and options['allocation_mode'] == 'balanced' and not urgent:
            floor_raw = math.ceil(options['balance_floor_percent'] * 63 / 100)
            for group in options['groups'][1:]:
                for i in group:
                    if (i not in (overrides or {}) and i in self.raw
                            and self.raw[i] < floor_raw
                            and targets[i] >= options['balance_floor_percent']):
                        prime.add(i)
        changes = {}
        for i in range(1, 7):
            desired = max(1, min(63, targets[i] * 63 / 100))
            requested = math.ceil(desired)
            current = self.raw.get(i)
            if current is None or urgent or i in force:
                candidate = requested
            elif i in prime:
                candidate = min(requested, floor_raw)
            elif now - self.changed_at[i] < options['output_hold_seconds']:
                candidate = current
            elif requested == 1 and current > 1:
                candidate = max(1, current - options['output_step_raw'])
            elif desired > current + options['output_hysteresis_raw']:
                candidate = min(requested, current + options['output_step_raw'])
            elif desired < current - 1 - options['output_hysteresis_raw']:
                candidate = max(requested, current - options['output_step_raw'])
            else:
                candidate = current
            if prime and current is not None and candidate < current and i not in (overrides or {}):
                candidate = current
            if candidate != current or i in force: changes[i] = candidate
        if refresh:
            command('raw', '0x30', '0xf9', '0xff', '0x00')
        for i in range(1, 7):
            if i in changes or refresh:
                value = changes.get(i, self.raw.get(i, math.ceil(targets[i] * 63 / 100)))
                command('raw', '0x30', '0xf9', hex(i), hex(value))
                if i in changes:
                    self.raw[i] = value
                    self.changed_at[i] = now
        if refresh: self.refreshed_at = now
        return {str(i): round(raw * 100 / 63, 1) for i, raw in self.raw.items()}

    def status(self):
        return {'fan_raw': {str(i): v for i, v in self.raw.items()},
                'fan_allocated_percent': {str(i): round(v, 2) for i, v in self.allocated.items()},
                'fan_stages_active': list(self.active)}


class CPULoad:
    """Host CPU busy time, including guests once; exclude idle, iowait and steal."""
    def __init__(self, path='/proc/stat'):
        self.path, self.previous, self.available = path, None, False

    def sample(self):
        try:
            lines = pathlib.Path(self.path).read_text().splitlines()
            fields = next(line.split()[1:] for line in lines if line.startswith('cpu '))
            values = tuple(int(v) for v in fields[:8])
            count = sum(line.split()[0][3:].isdigit() for line in lines if line.startswith('cpu'))
            if len(values) != 8 or any(v < 0 for v in values) or not count:
                raise ValueError('Invalid /proc/stat CPU counters')
        except (OSError, ValueError, StopIteration):
            self.previous, self.available = None, False
            return None
        previous, self.previous = self.previous, (values, count)
        self.available = False
        if previous is None or previous[1] != count:
            return None
        # iowait is documented to sometimes decrease; other regressions reset
        # the sample. guest/guest_nice are already counted in user/nice.
        deltas = [v - old for v, old in zip(values, previous[0])]
        if any(v < 0 for i, v in enumerate(deltas) if i != 4):
            return None
        deltas[4] = max(0, deltas[4])
        total = sum(deltas)
        if total <= 0:
            return None
        self.available = True
        return 100 * sum(deltas[i] for i in (0, 1, 2, 5, 6)) / total


class AdaptiveCPU:
    """Curve-guided load feed-forward with bounded prediction and timed trim.

    CPU utilization is a heat hint, not a power measurement. Temperature
    feedback learns a signed correction to its provisional airflow estimate.
    Never integrate temperature error every second or carry an emergency duty
    into the next normal target.
    """
    def __init__(self, temperature, now):
        self.temperature = self.filtered = self.predicted = temperature
        self.sample_time = self.last_trim = now
        self.rate = self.load = self.load_fast = self.load_slow = 0.0
        self.trim = self.curve_duty = self.load_duty = self.desired = 0.0
        self.trim_load = 0.0
        self.hot_since = self.cool_since = None
        self.signature = None
        self.state = 'starting'
        self.load_available = False

    def update(self, config, temperature, previous, now, load=None, learn=True):
        options = predictive_options(config)
        signature = json.dumps([config['cpu_curve'], config.get('curve_mode', 'linear'),
                                config.get('cpu_target_c', 60), options], sort_keys=True)
        if self.signature is not None and signature != self.signature:
            self.__init__(temperature, now)
        self.signature = signature
        elapsed = max(.001, now - self.sample_time)
        # A slow sensor read must not produce a catch-up burst of output changes.
        slew_time = min(5, elapsed)
        temperature_change = temperature - self.temperature
        measured_rate = temperature_change / elapsed
        old_filtered = self.filtered
        self.filtered += -math.expm1(-elapsed / options['temperature_filter_seconds']) * (temperature - self.filtered)
        filtered_rate = (self.filtered - old_filtered) / elapsed
        self.rate += -math.expm1(-elapsed / 10) * (filtered_rate - self.rate)
        self.temperature, self.sample_time = temperature, now
        self.load_available = load is not None and math.isfinite(load) and 0 <= load <= 100
        if self.load_available:
            # Feed-forward rises promptly and releases slowly after load ends.
            self.load_fast += -math.expm1(-elapsed / 3) * (load - self.load_fast)
            self.load_slow += -math.expm1(-elapsed / 30) * (load - self.load_slow)
            load_tau = 3 if load > self.load else 45
            self.load += -math.expm1(-elapsed / load_tau) * (load - self.load)
            if self.trim < 0 and self.load > self.trim_load + 15:
                self.trim = max(0, self.trim)  # do not reuse a low-load cooling correction
        load_lead = (max(0, self.load_fast - self.load_slow) / 100
                     * options['load_prediction_c']
                     if self.load_available and measured_rate >= 0 and filtered_rate >= 0 else 0)
        # Suppress residual positive trend as soon as the raw CPU starts cooling.
        trend_lead = (max(0, self.rate) * options['prediction_seconds']
                      if measured_rate >= 0 and filtered_rate >= 0 else 0)
        self.predicted = self.filtered + min(options['prediction_max_c'], trend_lead + load_lead)
        target = config.get('cpu_target_c', 60)
        band = options['deadband_c']
        full_speed = config['cpu_curve'][-1][0]
        if temperature >= full_speed:
            self.trim = 0
            self.curve_duty = 100
            self.load_duty = interpolate(options['load_curve'], self.load)
            self.hot_since = self.cool_since = None
            self.state, self.desired = 'emergency', 100
            return 100
        # Forecasts may select ordinary curve points, never the final emergency
        # point. Only raw temperature or hardware health forces full speed.
        mode = config.get('curve_mode', 'linear')
        curve_temperature = min(self.predicted, full_speed - .001)
        self.curve_duty = interpolate(config['cpu_curve'], curve_temperature, mode)
        self.load_duty = interpolate(options['load_curve'], self.load)
        baseline = max(self.curve_duty, self.load_duty)
        minimum_trim = max(-options['max_trim_percent'], self.curve_duty - baseline)
        # Anti-windup: once the JSON curve is the floor, extra negative
        # correction cannot affect output and must not accumulate invisibly.
        self.trim = max(minimum_trim, self.trim)
        # One-degree BMC quantization must not restart the settling timers on
        # every alternating reading. A clear drop or sustained trend still
        # stops warming corrections promptly.
        cooling = temperature_change <= -2 or self.rate < -.03 or filtered_rate < -.15
        warming = temperature_change >= 2 or self.rate > .03
        hot = (not cooling and self.filtered >= target - band
               and (self.filtered > target + band + .05
                    or (warming and self.predicted > target + band + .1)))
        cool = (not warming and temperature < target - band
                and self.filtered < target - band
                and self.load_fast <= self.load_slow + 5)
        self.hot_since = (now if self.hot_since is None else self.hot_since) if hot and learn else None
        self.cool_since = (now if self.cool_since is None else self.cool_since) if cool and learn else None
        self.state = 'hold'
        if (self.hot_since is not None
                and now - self.hot_since >= options['trim_interval_seconds']
                and now - self.last_trim >= options['trim_interval_seconds']):
            self.trim = min(options['max_trim_percent'], self.trim + options['trim_step_percent'])
            self.last_trim = self.hot_since = now
            self.trim_load = self.load
            self.state = 'step_up'
        elif (self.cool_since is not None
                and now - self.cool_since >= options['cooldown_hold_seconds']
                and now - self.last_trim >= options['cooldown_interval_seconds']):
            self.trim = max(minimum_trim, self.trim - options['trim_step_percent'])
            self.last_trim = now
            self.trim_load = self.load
            self.state = 'step_down'
        self.desired = min(100, max(self.curve_duty, baseline + self.trim))
        rise = options['rise_percent_per_second']
        if temperature >= full_speed - 3:
            # Smooth guard band as raw temperature approaches the hard endpoint.
            self.desired = max(self.desired, 100 * (temperature - full_speed + 3) / 3)
            rise = max(rise, 10)
            self.state = 'high_temperature'
        return min(previous + rise * slew_time,
                   max(self.desired, previous - options['fall_percent_per_second'] * slew_time))

    def status(self):
        return {'cpu_filtered_c': round(self.filtered, 2),
                'cpu_predicted_c': round(self.predicted, 2),
                'cpu_trend_c_per_second': round(self.rate, 4),
                'cpu_load_filtered_percent': round(self.load, 2),
                'cpu_load_available': self.load_available,
                'cpu_curve_demand_percent': round(self.curve_duty, 2),
                'cpu_load_demand_percent': round(self.load_duty, 2),
                'cpu_trim_percent': round(self.trim, 2),
                'cpu_desired_percent': round(self.desired, 2),
                'cpu_controller_state': self.state}

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
    adaptive = AdaptiveCPU(max(core_max, temperatures['CPU0 Temp'], temperatures['CPU1 Temp']),
                           time.monotonic())
    cpu_load = CPULoad()
    cpu_load.sample()  # establish counters without waiting or making BMC reads
    staged_fans = StagedFans()
    control = None
    try:
        control = ControlServer(pathlib.Path(STATE).parent / 'control.sock')
        notify('READY=1')
        while not stop:
            cycle_started = time.monotonic()
            config = read_config() # edits take effect without a restart
            pending, force = control.poll()
            tracked_fans.update(config['required_fans'])
            sensor_started = time.monotonic()
            try:
                sensors = read_sensors()
            except (subprocess.TimeoutExpired, RuntimeError) as error:
                sensors = {}
                sensor_timestamp = None
                read_error = str(error)
            else:
                sensor_timestamp = time.time()
                read_error = None
            sensor_duration = time.monotonic() - sensor_started
            # Remember any additional fans/DIMMs reconnected during this run.
            tracked_fans.update(n for n,s in sensors.items() if n.startswith('SYS_FAN') and (s['value'] or 0)>0)
            tracked_temps.update(n for n,s in sensors.items() if n.startswith('Temp_DIMM_') and s['value'] is not None)
            core_max, core_critical = core_temperatures()
            target,reasons,temperatures = demand(config,sensors,core_max,core_critical,tracked_fans,tracked_temps)
            if read_error:
                reasons.append('sensor read failed: ' + read_error)
            cpu_max = max(core_max, temperatures.get('CPU0 Temp', 0), temperatures.get('CPU1 Temp', 0))
            load = cpu_load.sample()
            predictive = config.get('cpu_control_mode', 'curve') in ('adaptive', 'predictive')
            if predictive and not reasons:
                target = adaptive.update(config, cpu_max, previous, time.monotonic(), load,
                                         learn=not control.overrides)
            else:
                adaptive = AdaptiveCPU(cpu_max, time.monotonic())
                adaptive.state = 'fault' if reasons else 'curve'
                adaptive.desired = target
                adaptive.load_available = load is not None
            demand_percent = adaptive.desired if predictive and not reasons else target
            # Never delay rising speed; let falling speed settle gradually.
            if not predictive:
                target = cooldown(target, previous, config['fall_percent_per_cycle'])
            emergency = bool(reasons) or cpu_max >= config['cpu_curve'][-1][0]
            urgent = cpu_max >= config['cpu_curve'][-1][0] - 3
            if staging_options(config)['enabled'] or control.overrides or force:
                fan_duties = staged_fans.apply(config, target, time.monotonic(),
                    emergency=emergency, urgent=urgent,
                    overrides=control.overrides, force=force)
                effective = max(fan_duties.values())
                fan_status = staged_fans.status()
            else:
                effective = manual(target)
                fan_duties = {str(i): effective for i in range(1, 7)}
                fan_status = {'fan_stages_active': [True] * 3}
                staged_fans = StagedFans()
            previous = target
            status = {'timestamp':time.time(),'ipmi_read_timestamp_seconds':sensor_timestamp,
                      'ipmi_read_duration_seconds':sensor_duration,'ipmi_sensors':sensors,
                      'duty_percent':effective,'target_percent':round(target,1),
                      'demand_percent':round(demand_percent,1),
                      'fan_duty_percent':fan_duties,
                      'fan_control_mode':'staged' if staging_options(config)['enabled'] else 'uniform',
                      'manual_overrides':{str(i):v for i,v in control.overrides.items()},
                      'control_mode':('manual' if len(control.overrides) == 6 else
                                      'mixed' if control.overrides else 'auto'),
                      'manual_safety_override':bool(control.overrides) and (emergency or urgent),
                      'cycle_work_seconds':round(time.monotonic()-cycle_started,3),
                      'cpu_control_mode':config.get('cpu_control_mode', 'curve'),
                      'cpu_target_c':config.get('cpu_target_c', 60),
                      'cpu_load_percent':round(load, 2) if load is not None else None,
                      'cpu_max_c':cpu_max,
                      'cpu_core_max_c':core_max,'temperatures_c':temperatures,'faults':reasons,
                      'rpm':{n:sensors.get(n,{}).get('value') for n in sorted(tracked_fans)},
                      'curve_sources':config['curve_sources'],
                      'cpu_only':config.get('cpu_only', False),
                      'curve_mode':config.get('curve_mode','linear')}
            status.update(adaptive.status())
            status.update(fan_status)
            temporary = pathlib.Path(STATE + '.tmp'); temporary.write_text(json.dumps(status,indent=2)); temporary.replace(STATE)
            control.acknowledge(pending, status)  # acknowledge after hardware writes/status publication
            exporter = '/usr/local/sbin/ipmi-metrics-5s.py'
            if pathlib.Path(exporter).is_file():
                try:
                    subprocess.run([exporter], check=True, timeout=2,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.SubprocessError):
                    print('Monitoring export failed; fan control continues', flush=True)
            event = (tuple(fan_duties.items()),tuple(reasons))
            if event != last_event or time.monotonic()-last_log>60:
                print('duty=%s%% fans=%s cpu=%sC faults=%s' % (effective,fan_duties,max(core_max,temperatures.get('CPU0 Temp',0),temperatures.get('CPU1 Temp',0)),reasons),flush=True)
                last_log = time.monotonic(); last_event = event
            notify('WATCHDOG=1\nSTATUS=Fan duty %s%%; CPU %s C' % (effective,core_max))
            end = time.monotonic()+remaining_delay(cycle_started, config['interval_seconds'])
            while not stop and time.monotonic()<end: time.sleep(.2)
    except Exception:
        if not stop:
            raise
    finally:
        try:
            automatic()
        finally:
            if control is not None: control.close()
        print('Restored native BMC automatic fan control',flush=True)

if __name__ == '__main__': main()
