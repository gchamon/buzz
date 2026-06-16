#!/bin/sh

# Read media server kind from buzz.yml (defaults to jellyfin).
config_path="${BUZZ_CONFIG:-/app/buzz.yml}"

# Trigger rclone VFS refresh via RC (internal network).
# We use a 2s timeout and ignore failures to keep the hook resilient.
curl -s -X POST --max-time 2 http://rclone:5572/vfs/refresh?recursive=true >/dev/null 2>&1 || true

media_server=$(python3 -c "
import sys, yaml
try:
    with open('$config_path') as f:
        data = yaml.safe_load(f) or {}
    print(((data.get('media_server') or {}).get('kind') or 'jellyfin').strip().lower())
except Exception:
    print('jellyfin')
")

case "$media_server" in
plex)
    exec bash /app/scripts/plex_update.sh "$@"
    ;;
jellyfin)
    # Curator handles Jellyfin scans natively via webhook; nothing to do here.
    echo "Jellyfin is NO-OP"
    exit 0
    ;;
*)
    echo "Unsupported media_server.kind: $media_server" >&2
    ;;
esac
