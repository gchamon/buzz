#!/bin/bash

# PLEX PARTIAL SCAN script.
# Triggered when buzz detects library changes via hooks.on_library_change.
# Plex support in Buzz is currently UNTESTED.
#
# Dependencies inside the Buzz container: bash, curl, xmllint, python3.

config_path="${BUZZ_CONFIG:-/app/buzz.yml}"

read_buzz_yml() {
    python3 -c "
import sys, yaml
try:
    with open('$config_path') as f:
        data = yaml.safe_load(f) or {}
    plex = ((data.get('media_server') or {}).get('plex') or {})
    print((plex.get('url') or '').rstrip('/'))
    print(plex.get('token') or '')
except Exception:
    print('')
    print('')
"
}

readarray -t plex_cfg < <(read_buzz_yml)
plex_url="${plex_cfg[0]}"
token="${plex_cfg[1]}"
library_mount="/mnt/buzz"

if [ -z "$token" ]; then
    echo "media_server.plex.token is not set in buzz.yml, skipping Plex update"
    exit 0
fi

if [ -z "$plex_url" ]; then
    echo "media_server.plex.url is not set in buzz.yml, skipping Plex update"
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
