#!/usr/bin/env ash

while true; do
    if ! ls /mnt/zurg/movies 2>&1 >/dev/null; then
        echo rclone mountpoint seems to be down, restarting...
        docker container restart plex
        docker container restart plex-healthcheck
    else
        echo rclone mountpoint seems to be working for now...
    fi
    sleep 10
done
