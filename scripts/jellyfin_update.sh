#!/bin/sh

# JELLYFIN UPDATE script
# When zurg detects changes, it can trigger this script IF your config.yml contains
# on_library_update: sh /app/media_update.sh "$@"

jellyfin_url="${JELLYFIN_URL:-http://<url>}"
api_key="${JELLYFIN_API_KEY:-<api-key>}"
scan_task_id="${JELLYFIN_SCAN_TASK_ID:-}"

if [ -z "$scan_task_id" ]; then
    tasks_json=$(curl -fsSL -H "Authorization: MediaBrowser Token=$api_key" \
        "$jellyfin_url/ScheduledTasks?IsHidden=false&IsEnabled=true") || exit 1

    scan_task_id=$(printf '%s' "$tasks_json" \
        | sed 's/},{/}\n{/g' \
        | grep '"Name"[[:space:]]*:[[:space:]]*"Scan Media Library"' \
        | sed -n 's/.*"Id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -n 1)
fi

if [ -z "$scan_task_id" ]; then
    echo "Unable to find the Jellyfin Scan Media Library task ID" >&2
    exit 1
fi

echo "Starting Jellyfin library scan task: $scan_task_id"
curl -fsSL -X POST \
    -H "Authorization: MediaBrowser Token=$api_key" \
    "$jellyfin_url/ScheduledTasks/Running/$scan_task_id"

echo "Jellyfin library scan triggered"
