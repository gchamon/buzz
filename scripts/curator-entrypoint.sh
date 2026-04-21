#!/bin/sh
# Ensure subtitle directories exist (volume may be freshly mounted)
mkdir -p /mnt/buzz/subs/movies /mnt/buzz/subs/shows /mnt/buzz/subs/anime
mkdir -p /mnt/buzz/curated
exec python3 -m buzz "$@"
