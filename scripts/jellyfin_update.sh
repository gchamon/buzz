#!/bin/sh

# JELLYFIN UPDATE script
# When buzz detects changes, it can trigger this script IF your config contains
# on_library_update: sh /app/media_update.sh "$@"

builder_url="${PRESENTATION_BUILDER_URL:-http://presentation-builder:8400/rebuild}"

echo "Rebuilding Jellyfin presentation library via: $builder_url"
curl -fsSL -X POST "$builder_url"
echo
echo "Jellyfin presentation library rebuild requested"
