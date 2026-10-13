#!/usr/bin/env bash
set -euo pipefail
((EUID == 0)) || { echo 'Run this script as root.' >&2; exit 1; }
systemctl disable --now quanta-fan-control.service
systemctl reset-failed quanta-fan-control.service
/usr/bin/ipmitool raw 0x30 0xf9 0xff 0x01
rm -f /usr/local/sbin/quanta-fan-control.py /etc/systemd/system/quanta-fan-control.service
rm -f /usr/local/sbin/fan-control /etc/init.d/fan
rm -f /etc/modules-load.d/quanta-fan-control.conf
systemctl daemon-reload
echo 'Native BMC automatic fan control restored. Configuration and backups retained.'
