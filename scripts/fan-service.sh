#!/bin/sh
### BEGIN INIT INFO
# Provides: fan
# Required-Start:
# Required-Stop:
# Default-Start:
# Default-Stop:
# Short-Description: Commands for the Quanta fan controller
### END INIT INFO
# Command-only compatibility wrapper; quanta-fan-control.service owns the daemon.
if [ "${1-}" = control ]; then shift; fi
exec /usr/local/sbin/fan-control "$@"
