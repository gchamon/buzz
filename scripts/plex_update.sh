#!/bin/bash

# PLEX PARTIAL SCAN script or PLEX UPDATE script
# When buzz detects changes, it can trigger this script IF your config contains
# on_library_update: sh /app/media_update.sh "$@"

# docker compose exec buzz apk add libxml2-utils
# sudo apt install libxml2-utils

plex_url="${PLEX_URL:-http://<url>}" # If you're using buzz inside a Docker container, by default it is 172.17.0.1:32400
token="${PLEX_TOKEN:-<token>}" # open Plex in a browser, open dev console and copy-paste this: window.localStorage.getItem("myPlexAccessToken")
library_mount="${LIBRARY_MOUNT:-/mnt/buzz}" # replace with your buzz mount path, ensure this is what Plex sees

# Get the list of section IDs
section_ids=$(curl -sLX GET "$plex_url/library/sections" -H "X-Plex-Token: $token" | xmllint --xpath "//Directory/@key" - | grep -o 'key="[^"]*"' | awk -F'"' '{print $2}')

for arg in "$@"
do
    parsed_arg="${arg//\\}"
    echo $parsed_arg
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
