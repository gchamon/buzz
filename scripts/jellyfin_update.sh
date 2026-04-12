#!/bin/sh

# JELLYFIN UPDATE script
# When buzz detects changes, it can trigger this script IF your config contains
# on_library_update: sh /app/media_update.sh "$@"

builder_url="${PRESENTATION_BUILDER_URL:-http://presentation-builder:8400/rebuild}"

echo "Rebuilding Jellyfin presentation library via: $builder_url"
response="$(curl --connect-timeout 5 --max-time 30 --fail-with-body -sS -X POST "$builder_url" 2>&1)"
status=$?
if [ "$status" -ne 0 ]; then
    printf '%s\n' "$response" >&2
    echo "Jellyfin presentation library rebuild failed" >&2
    exit "$status"
fi
printf '%s\n' "$response"
echo "Jellyfin presentation library rebuild requested"
