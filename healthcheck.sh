#!/usr/bin/env ash

target_container="${TARGET_CONTAINER:-plex}"
self_container="${SELF_CONTAINER:-${HOSTNAME}}"
healthcheck_path="${HEALTHCHECK_PATH:-/mnt/buzz/movies}"
healthcheck_verbose="${HEALTHCHECK_VERBOSE:-false}"

is_truthy() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

while true; do
    if ! ls "$healthcheck_path" 2>&1 >/dev/null; then
        echo rclone mountpoint seems to be down, restarting...
        docker container restart "$target_container"
        docker container restart "$self_container"
    else
        if is_truthy "$healthcheck_verbose"; then
            echo rclone mountpoint seems to be working for now...
        fi
    fi
    sleep 10
done
