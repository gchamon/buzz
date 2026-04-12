#!/usr/bin/env ash

target_container="${TARGET_CONTAINER:-plex}"
self_container="${SELF_CONTAINER:-${HOSTNAME}}"
healthcheck_path="${HEALTHCHECK_PATH:-/mnt/buzz/movies}"

while true; do
    if ! ls "$healthcheck_path" 2>&1 >/dev/null; then
        echo rclone mountpoint seems to be down, restarting...
        docker container restart "$target_container"
        docker container restart "$self_container"
    else
        echo rclone mountpoint seems to be working for now...
    fi
    sleep 10
done
