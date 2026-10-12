# Physical Host Audit: 2026-10-12

Scope: quanta1 and quanta2 Proxmox hosts. Guest operating systems were not
audited; temporary guest SSH credentials had already been revoked.

## Unresolved Findings

- CRITICAL: quanta1 /dev/sde reports SMART FAILED, 1228 reallocated sectors
  and 2 pending sectors. The nonredundant hdd-vg thin pool has its metadata
  on this disk and data across /dev/sde, /dev/sdc and /dev/sdd. VM100's data
  disk and VM101 depend on this pool. Back up the affected guests/data to
  independent storage and plan disk replacement/migration urgently.
- HIGH: quanta1 /dev/sdc has 8 pending sectors and 454 recorded UDMA CRC
  errors. Investigate disk health and cabling during the storage repair.
- HIGH: external authentication attempts appear in quanta1 Proxmox logs.
  Management/exporter services listen on wildcard addresses, including
  IPv6, without host firewall restrictions found during inspection.
  Restrict management access to verified trusted networks or VPN; do not
  apply blanket rules without confirming the administrator's access path.
- No local VM backups were found in /var/lib/vz/dump. External backups were
  not verified. Mail alias databases were rebuilt, but alert delivery was
  not tested.
- quanta1 forwarding is managed by network hooks, persistent iptables and
  dedicated services. Existing application routes were retained; network
  hooks were made idempotent. Consolidation needs a separate network plan.

## Cleanup Completed

- Disabled and removed quanta1's obsolete BMC HTTP proxy service.
- Corrected quanta2's hostname mapping to its assigned 10.10.10.2 address.
- Rebuilt both hosts' missing mail alias databases with newaliases.
- Removed reproducible BMC source downloads and completed scratch
  workspaces on quanta2 (approximately 565 MB).
- Archived quanta1 firmware research and monitoring workspace under
  /var/backups/server-audit-20261012, mode 0700. Removed stale BMC session
  cookie files. Firmware dumps and useful diagnostics were retained.
- Preserved the already-running shared IPMI monitoring integration in
  version control. The optional /usr/local/sbin/ipmi-metrics-5s.py helper
  and monitoring systemd overrides remain host-managed dependencies.

## Verification

Both fan controllers were active and their installed source matched the
repository source. Both hosts had no failed systemd units after cleanup.
The controller's 34 unit tests passed. Shared IPMI collection was healthy;
health and monitoring HTTPS endpoints returned their login redirects.
No storage, guest configuration, live forwarding or management firewall
changes were made.
