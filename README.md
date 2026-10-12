# Quanta S210 fan control

Temperature-based fan control exported from **quanta1**, packaged for installation
on **quanta2**. The controller, configuration, and systemd unit are the original
files exported on 12 October 2026; the installer and tests are added here.

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
below 500 RPM, or reports an alarm. Resolve the sensor/fan readings before retrying.
The snapshot has been validated locally; installation on quanta2 has not yet
been performed.

## Configuration

The reusable base configuration is
[`config/quanta-fan-control.base.json`](config/quanta-fan-control.base.json).
New installations use this file; existing `/etc/quanta-fan-control.json` files
are preserved. `config/quanta-fan-control.json` retains the original quanta1
snapshot. To install a tuned copy of the base, use `--config /path/to/config.json`.

Edit `/etc/quanta-fan-control.json`. Valid edits are read each cycle without a
restart. The exported configuration uses CPU-only, step-based control:

| CPU temperature | Requested fan duty |
| --- | --- |
| Below 50°C | 5% |
| 50°C to below 60°C | 10% |
| 60°C to below 65°C | 20% |
| 65°C and above | 100% |

Actual duty is rounded upward to the BMC's 1–63 scale. RPM is measured separately
and is not a direct percentage of maximum RPM. Falling duty is limited to three
percentage points per cycle; rising duty is applied immediately. Each cycle
includes sensor reads and IPMI commands, then the configured two-second delay.

Curve points are `[temperature, duty_percent]`. Temperatures must increase,
duty must not decrease, and each non-empty curve must end at **100%**. The CPU
full-speed temperature must be at most **68°C**. Empty curves inherit the first
non-empty curve, in CPU/DIMM/PCH/ambient/backplane/inlet/exhaust order. Set
`cpu_only` to `false` to include all curves in normal demand selection; the
highest demand wins. Temperature and fan health checks remain active in
CPU-only mode.

Missing/invalid temperatures, sensor alarms, fan faults, or CPU temperatures
within 10°C of the hardware critical threshold request 100% duty. Startup health
faults refuse activation. Configuration/read/command errors stop the controller;
the Python cleanup and systemd `ExecStopPost` restore BMC automatic control.
The systemd watchdog covers a stalled userspace controller, with automatic
control restored after watchdog termination. A host-wide freeze is outside that
watchdog's coverage.

An optional inventory cache at `/var/lib/quanta-fan-control/sdr-cache.bin` is used
when present. Live sensor readings are still fetched. No cache is required or
shipped; regenerate it after hardware inventory changes.

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
