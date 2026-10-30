#!/usr/bin/env bash
set -euo pipefail

# Bash is included in the official OTP image; curl/wget are not required.
port="${1:-8080}"
exec 3<>/dev/tcp/127.0.0.1/"$port"
printf 'GET /otp/actuators/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3
IFS= read -r status <&3
[[ "$status" == HTTP/*" 200 "* ]]
