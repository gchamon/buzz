#!/bin/sh

media_server="${MEDIA_SERVER:-plex}"

case "$media_server" in
plex)
    exec bash /app/scripts/plex_update.sh "$@"
    ;;
jellyfin)
    exec bash /app/scripts/jellyfin_update.sh "$@"
    ;;
*)
    echo "Unsupported MEDIA_SERVER: $media_server" >&2
    exit 1
    ;;
esac
