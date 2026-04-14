#!/bin/sh

# JELLYFIN UPDATE script
# This script refreshes the Jellyfin library after the curation layer is ready.

jellyfin_url="${JELLYFIN_URL:-http://jellyfin:8096}"
jellyfin_api_key="${JELLYFIN_API_KEY:-<token>}"

if [ -z "$jellyfin_api_key" ] || [ "$jellyfin_api_key" = "<token>" ]; then
    echo "JELLYFIN_API_KEY is not set, skipping library refresh"
    exit 0
fi

if [ -z "$jellyfin_url" ]; then
    echo "JELLYFIN_URL is not set, skipping library refresh"
    exit 0
fi

echo "Triggering Jellyfin library scan at: $jellyfin_url"

# Trigger a full library scan
response="$(curl --connect-timeout 5 --max-time 30 --fail-with-body -sS -X POST \
    "$jellyfin_url/Library/Refresh?api_key=$jellyfin_api_key" 2>&1)"
status=$?

if [ "$status" -ne 0 ]; then
    printf '%s\n' "$response" >&2
    echo "Jellyfin library refresh failed" >&2
    exit "$status"
fi

echo "Jellyfin library refresh requested"
