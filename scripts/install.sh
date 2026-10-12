#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
config_file=''
while (($#)); do
  case "$1" in
    --config)
      (($# >= 2)) || { echo '--config requires a file' >&2; exit 2; }
      config_file="$2"
      shift 2
      ;;
    -h|--help)
      echo 'Usage: sudo bash scripts/install.sh [--config PATH]'
      echo 'Preserves /etc/quanta-fan-control.json unless --config is supplied.'
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
((EUID == 0)) || { echo 'Run this installer as root.' >&2; exit 1; }
for tool in python3 modprobe systemctl install mktemp; do
  command -v "$tool" >/dev/null || { echo "Missing dependency: $tool" >&2; exit 1; }
done
[[ -x /usr/bin/ipmitool ]] || { echo 'Install ipmitool first: apt-get install ipmitool' >&2; exit 1; }
if [[ -z "$config_file" ]]; then
  if [[ -f /etc/quanta-fan-control.json ]]; then
    config_file=/etc/quanta-fan-control.json
  else
    config_file="$repo_dir/config/quanta-fan-control.base.json"
  fi
fi
[[ -f "$config_file" ]] || { echo "Configuration not found: $config_file" >&2; exit 1; }
# Freeze the selected configuration so the checked bytes are the installed bytes.
selected_config="$(mktemp)"
trap 'rm -f -- "$selected_config"' EXIT
cat -- "$config_file" > "$selected_config"
for module in ipmi_si ipmi_devintf coretemp; do modprobe "$module"; done
[[ -c /dev/ipmi0 ]] || { echo 'Local BMC device /dev/ipmi0 is unavailable.' >&2; exit 1; }
python3 "$repo_dir/scripts/preflight.py" --config "$selected_config"

backup_base=/var/backups/quanta-fan-control
install -d -m 0700 "$backup_base"
backup_dir="$(mktemp -d "$backup_base/$(date +%Y%m%d-%H%M%S).XXXXXX")"
files=(usr/local/sbin/quanta-fan-control.py etc/quanta-fan-control.json
       etc/systemd/system/quanta-fan-control.service etc/modules-load.d/quanta-fan-control.conf)
for file in "${files[@]}"; do
  if [[ -e "/$file" ]]; then
    install -d "$backup_dir/$(dirname -- "$file")"
    cp -a -- "/$file" "$backup_dir/$file"
  fi
done
if [[ -d /etc/systemd/system/quanta-fan-control.service.d ]]; then
  install -d "$backup_dir/etc/systemd/system"
  cp -a /etc/systemd/system/quanta-fan-control.service.d "$backup_dir/etc/systemd/system/"
fi
echo "Backup: $backup_dir"

if systemctl is-active --quiet quanta-fan-control.service; then
  systemctl stop quanta-fan-control.service
fi
install -m 0755 "$repo_dir/quanta-fan-control.py" /usr/local/sbin/quanta-fan-control.py
install -m 0644 "$selected_config" /etc/quanta-fan-control.json
install -m 0644 "$repo_dir/systemd/quanta-fan-control.service" /etc/systemd/system/quanta-fan-control.service
install -m 0644 "$repo_dir/modules-load.d/quanta-fan-control.conf" /etc/modules-load.d/quanta-fan-control.conf
systemctl daemon-reload
if systemctl is-failed --quiet quanta-fan-control.service; then
  systemctl reset-failed quanta-fan-control.service
fi
if ! systemctl enable --now quanta-fan-control.service; then
  systemctl stop quanta-fan-control.service || true
  /usr/bin/ipmitool raw 0x30 0xf9 0xff 0x01
  echo 'Activation failed; native BMC automatic fan control restored.' >&2
  exit 1
fi
systemctl status quanta-fan-control.service --no-pager --full
