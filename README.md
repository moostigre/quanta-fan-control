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
Both physical hosts run the controller; deployment verifies live health and
keeps a backup of the previous controller/configuration.

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
full-speed temperature must be at most **80°C**. The separate coretemp
hardware-critical-minus-10°C override still applies. Empty curves inherit the first
non-empty curve, in CPU/DIMM/PCH/ambient/backplane/inlet/exhaust order. Set
`cpu_only` to `false` to include all curves in normal demand selection; the
highest demand wins. Temperature and fan health checks remain active in
CPU-only mode.

### Quieter compilation profile (65–70°C target)

[`config/quanta-fan-control.quiet-load.json`](config/quanta-fan-control.quiet-load.json)
targets **67°C with a ±2°C band**, using the hottest core/package or BMC CPU
reading. `cpu_target_c` accepts 40–70°C and must remain at least 3°C below the
configured full-speed point. This profile requests 8% at 60°C, 12% at 65°C,
16% at 68°C, 22% at 70°C, and increasing cooling up to full speed at **78°C**.
The three-degree urgent guard begins at 75°C. CPU hardware-critical margin,
missing sensors, alarms, failed fans, watchdog and automatic-BMC fallback
remain active. Existing profiles retain their full-speed endpoints.

The BMC CPU readings can be hotter than the core graph: in the supplied
compilation window, cores reached 64°C while BMC CPU temperature reached 68°C.
The old 68°C endpoint triggered 100% duty and approximately 20,400 RPM. Replaying
those recorded temperatures with this profile produced a 16% maximum request.
Replay evaluates control decisions on recorded observations; it does not
predict the temperature response to different airflow.

The new profile reduces the provisional load estimate to 14% at full utilization,
rises at 0.5 percentage points per second, and falls at 2 points per second.
`predictive.load_release_seconds` is 8 seconds (45 by default for existing
profiles). A learned positive hot-load correction releases at the output fall
rate once both raw and filtered CPU temperatures are more than 5°C below target.
Normal below-target learning also releases sooner in this profile.

Fan staging keeps small upward steps (one raw level every five seconds), but
allows downward steps of three raw levels every two seconds through
`output_fall_step_raw` and `output_fall_hold_seconds`. Omitted falling options
inherit the configured upward limits, preserving existing profiles. Helpers
release after 15 seconds. A combined controller/PWM regression test verifies
recovery from full speed to near-idle within one minute after a drop to 45°C.

Install explicitly on each physical host, after checking live sensors:

```bash
bash scripts/install.sh --config config/quanta-fan-control.quiet-load.json
```

Use a real compilation and live temperature/RPM telemetry to validate the new
thermal equilibrium. Fixed-observation replay and the qualitative thermal model
are regression checks, not a calibrated model of either physical server.

### Load-aware predictive CPU control

Use `cpu_control_mode: "predictive"`, `cpu_only: true`, and `cpu_target_c: 60`.
Existing `"adaptive"` configurations now use this same controller: upgrades
replace the old per-second error accumulator and rapid cooldown. `"curve"`
mode keeps its original step/linear behavior. The installer preserves existing
configuration, so an existing adaptive installation adopts the new behavior
without replacing its temperature curve or full-speed endpoint.

[`config/quanta-fan-control.predictive.json`](config/quanta-fan-control.predictive.json)
is a starting profile matching quanta2's supplied curve: 5% at 50 C, 10% at
60 C, 15% at 65 C, 17% at 66 C, 20% at 67 C, and immediate 100% at 68 C.
Install it explicitly only when replacing configuration is intended:

```bash
bash scripts/install.sh --config config/quanta-fan-control.predictive.json
```

The controller samples host CPU utilization from `/proc/stat` each cycle,
including virtual-machine CPU work. It excludes idle, iowait and steal, and
avoids counting guest time twice. CPU utilization is an early heat hint,
**not a measurement of power**: instruction mix, frequency and package load
imbalance can produce different temperatures at the same utilization.

The provisional load-to-duty curve is linear between these points:

| Host CPU utilization | Provisional duty |
| --- | --- |
| 0% | 1% |
| 25% | 8% |
| 50% | 15% |
| 75% | 22% |
| 100% | 28% |

Load filtering rises with a three-second time constant and releases with a
45-second time constant. Temperature is filtered over five seconds; its
filtered warming trend predicts ten seconds ahead. A rising-load hint adds
bounded anticipation, and the entire prediction is capped at three degrees.
Residual warming prediction is suppressed when raw temperature starts falling.
The predicted temperature selects ordinary JSON curve points; prediction alone
cannot select the final emergency point.

Normal demand is the larger of the curve demand and provisional load demand,
plus a signed, bounded learned correction. The curve remains a lower bound:
negative correction can reduce an excessive load estimate, but cannot remove
configured thermal airflow. Above the 59-61 C target band, while not cooling,
correction increases by one BMC level (100/63, about 1.6 percentage points) at
most once per 15 seconds. Prediction can trigger a correction while warming
near the target, but stable temperature within the band does not accumulate
error. Below 59 C for 30 seconds, while not warming and without a rising-load
hint, correction decreases by one level at most once per 20 seconds. Correction
is limited to +/-30 percentage points. Larger new loads discard corrections
learned at low load. Normal output rises at most one percentage point per second
and falls at most 0.25 per second; a long read cannot accumulate a burst of
missed adjustments. The unrounded request is retained so BMC quantization does
not stall small changes.

Within three degrees of the full-speed endpoint, a separate raw-temperature
guard raises demand toward 100% and permits faster acceleration. Reaching the
configured endpoint, sensor faults, and the hardware critical margin request
100% immediately. Emergency duty is not retained as the next normal target;
recovery approaches normal demand using the fall limit. CPU-load read errors,
counter resets and CPU hotplug re-establish the utilization baseline; temperature
control continues and retains the last load estimate until a fresh sample is
available. Configuration edits reset prediction and learned correction.

Tune the following optional `predictive` object; omitted fields use defaults:

| Setting | Default | Purpose |
| --- | --- | --- |
| `load_curve` | Table above | `[CPU utilization %, duty %]` points spanning 0-100% |
| `temperature_filter_seconds` | 5 | Temperature filter time constant |
| `prediction_seconds` | 10 | Warming-trend horizon |
| `prediction_max_c` | 3 | Maximum total prediction above filtered temperature |
| `load_prediction_c` | 3 | Maximum rising-load anticipation |
| `deadband_c` | 1 | Target temperature tolerance |
| `trim_step_percent` | 100/63 | Single correction step |
| `trim_interval_seconds` | 15 | Minimum warming correction interval |
| `cooldown_hold_seconds` | 30 | Continuous cool condition before reducing correction |
| `cooldown_interval_seconds` | 20 | Minimum cooling correction interval |
| `rise_percent_per_second` | 1 | Normal output rise limit |
| `fall_percent_per_second` | 0.25 | Normal output fall limit |
| `max_trim_percent` | 30 | Absolute correction bound |

`fall_percent_per_cycle` applies only in curve mode. The load table is an initial
estimate, not a calibrated duty/RPM or cooling-power map. Validate steady load,
load removal and abrupt load changes on each host; 20-30% duty is not a hard cap
or a guarantee of a 60 C ceiling. Keep raw-temperature safety active throughout.

Status preserves the shared IPMI sample and exporter hook, and adds
`cpu_load_percent`, `cpu_load_available`, `cpu_load_filtered_percent`,
`cpu_filtered_c`, `cpu_predicted_c`, `cpu_trend_c_per_second`,
`cpu_curve_demand_percent`, `cpu_load_demand_percent`, `cpu_trim_percent`,
`cpu_desired_percent`, and `cpu_controller_state`. `demand_percent` is the
pre-slew desired duty; `target_percent` is the request sent to the BMC.

For an offline decision replay, supply CSV columns
`timestamp,temperature_c,cpu_load_percent` (increasing timestamps in seconds;
blank load means unavailable):

```bash
python3 scripts/replay-predictive.py --config config/quanta-fan-control.predictive.json --input samples.csv > decisions.csv
```

This never accesses IPMI. Recorded temperatures reflect the old fan commands;
replay cannot prove the thermal response of the new commands. Regression tests
also exercise a qualitative delayed thermal model, not a calibrated host model.
Linux documents the CPU counters in [proc_stat(5)](https://man7.org/linux/man-pages/man5/proc_stat.5.html).

### Balanced CPU fan pairs

[`config/quanta-fan-control.staged.json`](config/quanta-fan-control.staged.json)
uses early assistance and balances cooling across neighbouring modules. The
CPU-facing order remains 3/4 first, then 2/5 (two fans per CPU), then 1/6:

| Stage | Fan modules | Deployed activation |
| --- | --- | --- |
| Primary CPU airflow | 3 and 4 | Always available |
| Neighbouring CPU airflow | 2 and 5 | Demand above 22% for 5 seconds |
| Outer assistance | 1 and 6 | Demand above 34% for 5 seconds, after neighbours |

The configured thresholds are `[0, 20, 32]` with two percentage points of
hysteresis. These are scalar **cooling-demand** thresholds, not RPM percentages
or per-fan commands. A stage releases only after demand remains below its
threshold minus two points for 60 seconds. This engages more fans before the
primary pair needs high RPM and avoids repeatedly switching helpers in and out.

`allocation_mode: "balanced"` shares demand **above**
`balance_floor_percent: 20` across active pairs. Recent quanta1 metrics showed
little RPM increase through roughly 20% PWM, then progressively higher RPM;
splitting a 30% request into two 15% requests would therefore leave both near
minimum rotation. The floor is an initial hardware estimate, not a calibrated
airflow model or fixed minimum speed. Below it, normal low demand still applies.
`priority_bias_percent: 2` gives earlier pairs a small preference. For example,
a sustained demand of 30% allocates about 26% to each of 3/4 and 24% to each of
2/5; demand of 40% with all stages active allocates about 28.7%, 26.7%, and 24.7%
to the respective pairs. Hardware rounding and held output transitions can
produce slightly different effective values. The existing predictive temperature
feedback continues to learn the cooling demand needed near the 60 C target.

New helpers are primed to the estimated PWM floor before reducing existing
cooling; RPM-producing increments then follow the normal hold/step limits.
The deployed profile moves at most **one raw BMC level (about 1.6 percentage
points) every five seconds** per module, except floor priming and heat/fault
protection. Quarter-level output hysteresis suppresses quantization chatter;
the complete output vector is refreshed every 30 seconds. Helpers below the
activation threshold get `idle_percent` (1%, raw level 1). Idle means minimum
rotation, not fan stop; the observed floor is roughly 4200/2900 RPM.

Within three degrees of the configured full-speed endpoint, all fans assist
without allocation delays or output holds. The endpoint or any hardware fault
still broadcasts 100% immediately. Low RPM is an ordinary-operation goal, not
a cap that can defeat heat protection. Manual per-fan commands remain supported;
manual helpers are not floor-primed or redistributed, and heat protection still
wins. Staging requires `cpu_only: true`.

Options under `fan_staging`: `enabled`, `groups`, `activation_percent`,
`allocation_mode` (`balanced` or historical `priority`), `balance_floor_percent`,
`priority_bias_percent`, `idle_percent`, `stage_hysteresis_percent`,
`stage_up_hold_seconds`, `stage_down_hold_seconds`, `output_hold_seconds`,
`output_step_raw`, `output_hysteresis_raw`, and `refresh_seconds`.
Defaults retain 10/30-second activation/release holds and two-level output steps;
the packaged/deployed balanced profile explicitly uses the gentler settings above.
Configuration changes preserve known outputs while resetting activation timers.

`groups` must cover selectors 1-6 exactly once in three pairs. The installed
mapping is `[[3,4],[2,5],[1,6]]`; `[[2,4],[3,5],[1,6]]` is also supported if a
physical airflow test shows that order works better. Fan placement comes from
the operator's observations; selector/register mapping is documented below.

Status includes `fan_allocated_percent` (automatic targets before manual
commands and output holds), effective `fan_duty_percent`, `fan_raw`,
`fan_stages_active`, `cpu_max_c` and per-rotor RPM. In staged mode
`target_percent` is scalar cooling demand; `duty_percent` is the largest effective
module duty. `service fan control status` now includes temperatures, target,
load, active stages and RPM for stress-test monitoring.

The earlier 13 October tests also showed `SYS_FAN5-1` RPM cycling even when
requested duty was stable. Reduced output writes and slower stage release address
software chatter; they cannot establish or repair a hardware/BMC cause of rotor
cycling. Check both rotor channels while testing the newly enlisted fans.

### Stress testing on either physical host

Run in the **Proxmox host shell**, not inside a Debian VM. Both hosts currently
have 24 logical CPUs and `stress-ng` installed. These tests use CPU IDs 2-23 so
two logical CPUs are excluded from the synthetic load. Stop a foreground test
with Ctrl-C. The timeouts stop each test automatically.

```bash
service fan control set auto on
# Moderate load, then sustained load; run sequentially.
taskset -c 2-23 stress-ng --cpu 22 --cpu-load 50 --cpu-method matrixprod --timeout 3m --metrics-brief
taskset -c 2-23 stress-ng --cpu 22 --cpu-load 100 --cpu-method matrixprod --timeout 10m --metrics-brief
# Leave the machine unloaded for a few minutes to observe the slow release.
```

In a second terminal:

```bash
watch -n 2 'service fan control status'
```

Use Grafana to compare CPU temperature, RPM on fans 2/3/4/5, the outer fans, and
faults through load start, steady load and removal. Expect neighbours to help
before the primary pair reaches its former RPM peaks, with CPU temperatures
settling near the target. Actual airflow and temperature response require this
live test; unit tests verify allocation/timers/safety, not a physical thermal
model. Review CPU IDs and worker counts after the planned CPU upgrade.

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

## Manual fan commands

Run as root (or prefix `sudo`). The requested command spelling is supported:

```bash
service fan control set fan1 100%
service fan control set fans 100%
service fan control set auto on
```

`fan1` through `fan6` select physical modules, each with both rotor channels.
`fans` selects all six. An individual manual setting leaves the other modules
under their configured automatic control. Acceptable duties are 1-100%, with
optional decimal values and `%`; the hardware rounds to its 1-63 PWM scale.
Zero is rejected because this interface has not provided a verified fan-stop
function. Explicit manual changes apply immediately, bypassing normal output
holds. No direct IPMI command is issued by the client.

`set auto on` clears every manual override and resumes the currently configured
predictive/staged or curve controller; it also starts that service if stopped.
It does **not** select native BMC automatic control. To release just one module:

```bash
service fan control set fan3 auto
service fan control status
```

The same operations are available as `fan-control set fan1 100%`,
`fan-control set fans 100%`, `fan-control set auto on`, and `fan-control status`.
The `fan` init script is a command wrapper; the supervised daemon remains
`quanta-fan-control.service`.

The running daemon accepts commands through its root-only UNIX socket and
acknowledges them after applying hardware output and publishing status. It
continues sensor/fan monitoring during manual operation. Thermal guard bands
can raise a manual setting; the configured full-speed endpoint and hardware
faults force all six modules to 100%, regardless of requested duty. Replies
report effective duties and any active safety override. Manual values remain
requested and take effect again when the safety override clears.

Overrides are held in memory, never written into the editable JSON. They last
until released or until the daemon restarts/reboots. Prediction continues while
manual overrides exist, but temperature correction learning pauses so manually
added airflow cannot teach the automatic controller an incorrect cooling
correction. Status adds `control_mode` (`auto`, `mixed`, or `manual`),
`manual_overrides`, and `manual_safety_override` alongside per-module duty.
Existing fault cleanup and watchdog restoration to native BMC control remain.

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

The zero-duty test now records bank selection, output styles, and fan-1 duty
readback alongside RPM. On quanta2, register 0xb6 read 16 before the zero
phase, 0 in all six zero-phase samples, and 16 after restoration. Both output
style registers remained zero, and the bank remained zero. Fan-1 rotors
settled around 4100-4200/2800 RPM despite zero duty readback, then returned to
5400/3800 RPM at raw 16. CPU readings stayed 38-41 C. This rules out a persistent
BMC rewrite of that duty register during these samples, but does not measure
the electrical PWM waveform or prove which downstream component enforces
the minimum. Normal controller operation was restored after the experiment.

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
