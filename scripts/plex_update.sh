#!/bin/bash

# PLEX PARTIAL SCAN script or PLEX UPDATE script
# When buzz detects changes, it can trigger this script IF your config contains
# on_library_update: sh /app/media_update.sh "$@"

# Dependencies inside the Buzz container: bash, curl, xmllint

plex_url="${PLEX_URL:-http://<url>}"
token="${PLEX_TOKEN:-<token>}"
library_mount="${LIBRARY_MOUNT:-/mnt/buzz}"

if [ -z "$PLEX_TOKEN" ] || [ "$token" = "<token>" ]; then
    echo "PLEX_TOKEN is not set, skipping Plex update"
    exit 0
fi

if [ -z "$PLEX_URL" ] || [ "$plex_url" = "http://<url>" ]; then
    echo "PLEX_URL is not set, skipping Plex update"
    exit 0
fi

echo "Fetching Plex sections from $plex_url..."

# Get the list of section IDs
response=$(curl -sLX GET "$plex_url/library/sections" -H "X-Plex-Token: $token")
if [ -z "$response" ]; then
    echo "Error: Plex returned an empty response" >&2
    exit 1
fi

section_ids=$(echo "$response" | xmllint --xpath "//Directory/@key" - 2>/dev/null | grep -o 'key="[^"]*"' | awk -F'"' '{print $2}')

if [ -z "$section_ids" ]; then
    echo "Warning: No Plex sections found or failed to parse XML"
fi

for arg in "$@"
do
    parsed_arg="${arg//\\}"
    echo "$parsed_arg"
    modified_arg="$library_mount/$parsed_arg"
    echo "Detected update on: $arg"
    echo "Absolute path: $modified_arg"

    for section_id in $section_ids
    do
        echo "Section ID: $section_id"

        curl -G -H "X-Plex-Token: $token" --data-urlencode "path=$modified_arg" $plex_url/library/sections/$section_id/refresh
    done
done

echo "All updated sections refreshed"

# credits to godver3, wasabipls
