# Quanta S210 fan control

Temperature-based fan control exported from **quanta1**, packaged for installation
on **quanta2**. Based on the files exported on 12 October 2026, with reusable
configuration, installation scripts, and tests. The original quanta1 configuration
snapshot and systemd unit are retained; the controller permits requested duties
down to 1% and RPM floors down to 100.

The controller uses local `ipmitool` commands on the physical Proxmox host.
Its hardware check requires S210-X12RS BMC firmware **7.54**, manufacturer ID
**7244**, product ID **12882**, and two Intel `coretemp` devices. The packaged
configuration requires all twelve `SYS_FAN1-1` through `SYS_FAN6-2` channels.

## Install on quanta2

Run in quanta2's Proxmox shell:

```bash
apt-get update
apt-get install --yes git python3 ipmitool
git clone https://github.com/moostigre/quanta-fan-control.git
cd quanta-fan-control
bash scripts/install.sh
```

The installer loads the required kernel modules, checks the BMC identity and
sensor health without changing fan duty, backs up existing files under
`/var/backups/quanta-fan-control/`, installs the files, and enables the service
at boot. If a configuration already exists, it is preserved; select a different
file explicitly with `bash scripts/install.sh --config /path/to/config.json`.
Existing systemd drop-ins are backed up and retained.

The earlier quanta2 exporter reported zero RPM on its fan channels. The installer
checks live BMC readings and refuses activation if a required fan is missing,
below the configured minimum (100 RPM in the base configuration), or reports an
alarm. Resolve the sensor/fan readings before retrying.
The snapshot has been validated locally; installation on quanta2 has not yet
been performed.

## Configuration

The reusable base configuration is
[`config/quanta-fan-control.base.json`](config/quanta-fan-control.base.json).
New installations use this file; existing `/etc/quanta-fan-control.json` files
are preserved. `config/quanta-fan-control.json` retains the original quanta1
snapshot. To install a tuned copy of the base, use `--config /path/to/config.json`.

Edit `/etc/quanta-fan-control.json`. Valid edits are read each cycle without a
restart. The base configuration uses CPU-only, step-based control:

| CPU temperature | Requested fan duty |
| --- | --- |
| Below 50°C | 1% |
| 50°C to below 60°C | 10% |
| 60°C to below 65°C | 20% |
| 65°C and above | 100% |

Actual duty is rounded upward to the controller's BMC 1–63 scale: a 1% request
uses raw level 1 and is reported as **1.6% effective duty**. RPM is measured separately
and is not a direct percentage of maximum RPM. Falling duty is limited to three
percentage points per cycle (or the configured `fall_percent_per_cycle`); rising
duty is applied immediately. Cooldown tracks the unrounded request so small
decrements accumulate across BMC steps. `interval_seconds` includes sensor reads
and IPMI commands; only the remaining time is slept. Hardware reads may still
take longer than the requested interval. Status includes `demand_percent`
before cooldown and `cycle_work_seconds` for diagnosing timing.

Curve points are `[temperature, duty_percent]`. Temperatures must increase,
duty must remain within **1–100%** and must not decrease, and each non-empty curve
must end at **100%**. `minimum_rpm` accepts **100–5000 RPM** and defaults to 100 in
the base configuration. Existing installations keep their selected configuration.
The CPU
full-speed temperature must be at most **68°C**. Empty curves inherit the first
non-empty curve, in CPU/DIMM/PCH/ambient/backplane/inlet/exhaust order. Set
`cpu_only` to `false` to include all curves in normal demand selection; the
highest demand wins. Temperature and fan health checks remain active in
CPU-only mode.

### Adaptive CPU temperature control

Set `cpu_control_mode` to `"adaptive"` and `cpu_target_c` to `60` with
`cpu_only: true` to replace normal curve steps with temperature feedback.
The controller uses the hottest core or BMC CPU reading. It filters the
temperature trend over roughly ten seconds. Only within two degrees of the
target, while the latest reading is still warming, it projects five seconds
ahead, capped at two degrees of prediction. Predicted temperatures above the
target progressively increase requested duty, capped at two percentage points
per second. Actual temperatures above 60.5 C also request more airflow.
Stable readings in the 58-60 C band hold duty even if a past load spike left a
positive filtered trend. Below 58 C, when not warming, duty falls at one to
five percentage points per second depending on distance below target.
Adaptive cooldown uses actual elapsed time rather than `fall_percent_per_cycle`,
which applies only in curve mode. BMC commands still update once per completed
sensor cycle; a three-second cycle applies three seconds of cooldown at once.

The first CPU curve duty is the minimum airflow; its final temperature still
forces 100% immediately. Sensor faults and the hardware critical margin also
override feedback with full speed. A 60 C target is not a guaranteed ceiling:
sudden load changes, slow BMC reads, and fan response can cause brief overshoot.
Observe behavior under representative load before relying on these defaults.
Status reports `cpu_trend_c_per_second` and `cpu_predicted_c`. Omit
`cpu_control_mode`, or set it to `"curve"`, to use the original step/linear mode.

Missing/invalid temperatures, sensor alarms, fan faults, or CPU temperatures
within 10°C of the hardware critical threshold request 100% duty. Startup health
faults refuse activation. Runtime IPMI sensor-read errors request full speed
and retry on the next cycle without resetting cooldown through a restart.
Configuration, core-temperature-read, and fan-command errors stop the controller;
the Python cleanup and systemd `ExecStopPost` restore BMC automatic control.
The systemd watchdog covers a stalled userspace controller, with automatic
control restored after watchdog termination. A host-wide freeze is outside that
watchdog's coverage.

An optional inventory cache at `/var/lib/quanta-fan-control/sdr-cache.bin` is used
when present. Live sensor readings are still fetched. No cache is required or
shipped; regenerate it after hardware inventory changes.

Avoid frequent competing IPMI collectors: they can slow BMC access and cause
timeouts. Quanta2's separate node-exporter IPMI collector is kept at one minute;
ordinary Prometheus scraping can still run every five seconds, but those cached
hardware metrics only update when the collector completes.

## Inspect and recover

```bash
systemctl status quanta-fan-control.service --no-pager --full
journalctl --unit=quanta-fan-control.service --boot --lines=80 --no-pager
cat /run/quanta-fan-control/status.json
/usr/local/sbin/quanta-fan-control.py --check
```

`status.json` includes effective `duty_percent`, target duty, CPU temperatures,
fan RPM, and fault reasons. This is the source for fan percentage; the existing
node exporter RPM metric does not expose it. `--check` reports health faults in
its JSON output; the installer additionally treats non-empty faults as failure.

If edits cause `final point must be 100%`, correct the last percentage of the
affected curve, then run these as separate commands:

```bash
systemctl reset-failed quanta-fan-control.service
systemctl restart quanta-fan-control.service
```

To return to BMC automatic control and disable the custom controller:

```bash
systemctl disable --now quanta-fan-control.service
/usr/bin/ipmitool raw 0x30 0xf9 0xff 0x01
```

`bash scripts/uninstall.sh` also removes the installed controller, main unit,
and module-loading configuration, retaining the editable config and backups.
Kernel modules already loaded are left in place. To roll back an upgrade, stop
the controller, restore the saved files from the displayed backup directory,
run `systemctl daemon-reload`, and enable/start the restored service.

## Validation

### Individual fan mapping test

On verified S210 firmware 7.54, selector 1 of `0x30 0xf9` was tested on
quanta2: `[0x01, 0x10]` raised only `SYS_FAN1-1` and `SYS_FAN1-2` from about
4200/2900 RPM to 5400/3800 RPM. Other modules remained near baseline.
This establishes module-1 selection. A subsequent guarded zero-duty test on
quanta2 accepted `[0x01, 0x00]`, but neither rotor stopped: after about seven
seconds they settled near 4200/2900 RPM. Setting raw duty 16 again raised them
to 5400/3800 RPM. CPU temperatures remained 38-42 C. Zero duty therefore does
not provide a fan-stop function through this interface on the tested system;
the test does not distinguish a BMC clamp from the fans' internal minimum.

`python3 scripts/test-fan-mapping.py` repeats this increased-airflow-only test
with health guards and an independent 90-second systemd restore timer. It
temporarily stops the normal controller and restores it on completion. Results
are written to `/root/quanta-firmware-research/fan1-mapping-test.json`; create
that directory before running. The test requires the normal controller to be
active and refuses to proceed when CPU temperatures reach 55 C or fans report
faults. It must not be run concurrently with another manual fan test.

`python3 scripts/test-fan-mapping.py --stop-fan1` explicitly enables the brief
zero-duty experiment: all other fans are kept at raw duty 16, only fan 1 may
report low-speed alarms during the zero phase, temperature increases above
five degrees abort the test, and nonzero fan-1 operation is checked afterward.
Results are saved separately as `fan1-zero-duty-test.json`. This option is
experimental and does not configure a permanent idle-stop policy.

### Firmware inspection findings

Offline inspection of the S210-X12RS V2 firmware 7.54 library shows that
`QuantaSetFanSpeedCtrl` passes raw duties 0-63 to `SetSysFanDuty`. That function
writes the requested byte directly through I2C bus 0 to address 0x2f, without
a minimum-duty clamp in this path. Its selector/register mapping is:

| Fan selector | W83793 bank-0 duty register |
| --- | --- |
| 1 | 0xb6 |
| 2 | 0xb7 |
| 3 | 0xb8 |
| 4 | 0xb9 |
| 5 | 0xba |
| 6 | 0xb5 |

`python3 scripts/inspect-fan-controller.py` reads these registers through IPMI
Master Write-Read, writing only the register pointer, not configuration values.
It refuses unexpected BMC identity or bank selection and never switches banks.
Live quanta2 readings showed bank 0, output-style registers 0xb0/0xb1 both zero
(open-drain PWM), and fan-1 duty register 0xb6 equal to 1.

The generic AMI `disable_fan_control` library function targets the AST PWM/TACH
driver; this is not evidence of a power switch for the external W83793-driven
fans. `FanSpeedControlSet` toggles automatic control rather than fan power.
No verified per-module power-cut command has been identified. The W83793's
SmartFan NonStop/StopTime settings describe automatic PWM behavior, not a
documented switch for disconnecting the fans' 12 V supply. Do not alter GPIOs
or output modes without establishing board wiring and fan electrical behavior.

These tests mock hardware access and require only Python's standard library:

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/install.sh scripts/uninstall.sh
```

For a read-only check of a proposed configuration on supported hardware:

```bash
python3 scripts/preflight.py --config config/quanta-fan-control.base.json
```

Reference: [ipmitool system-interface documentation](https://github.com/ipmitool/ipmitool#system-interface).
