#!/bin/sh

# JELLYFIN UPDATE script
# This script refreshes the Jellyfin library after the curation layer is ready.

jellyfin_url="${JELLYFIN_URL:-http://jellyfin:8096}"
jellyfin_token="${JELLYFIN_TOKEN:-<token>}"

if [ -z "$jellyfin_token" ] || [ "$jellyfin_token" = "<token>" ]; then
    echo "JELLYFIN_TOKEN is not set, skipping library refresh"
    exit 0
fi

echo "Triggering Jellyfin library scan at: $jellyfin_url"

# Trigger a full library scan
response="$(curl --connect-timeout 5 --max-time 30 --fail-with-body -sS -X POST \
    "$jellyfin_url/Library/Refresh?api_key=$jellyfin_token" 2>&1)"
status=$?

if [ "$status" -ne 0 ]; then
    printf '%s\n' "$response" >&2
    echo "Jellyfin library refresh failed" >&2
    exit "$status"
fi

echo "Jellyfin library refresh requested"
