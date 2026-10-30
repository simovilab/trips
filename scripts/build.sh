#!/usr/bin/env bash
set -euo pipefail

# Stage configuration without nesting a bind mount inside the data bind mount.
cp /opt/otp/otp-config.json /var/opentripplanner/otp-config.json
exec /docker-entrypoint.sh "$@"
