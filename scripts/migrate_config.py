#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import yaml
from pathlib import Path


DEFAULT_ANIME_PATTERN = r"\b[a-fA-F0-9]{8}\b"
DEFAULT_HOOK = "sh /app/media_update.sh"


def parse_zurg_config(raw: str) -> dict:
    config: dict[str, object] = {
        "directories": {
            "anime": {"filters": []},
            "shows": {"filters": []},
            "movies": {"filters": []},
        }
    }
    top_section: str | None = None
    directory_name: str | None = None
    in_filters = False
    current_filter: dict[str, str] | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))

        if indent == 0 and stripped.endswith(":"):
            key = stripped[:-1]
            if key == "directories":
                top_section = "directories"
                directory_name = None
                in_filters = False
                current_filter = None
                continue

        if top_section == "directories":
            if indent == 2 and stripped.endswith(":"):
                candidate = stripped[:-1]
                if candidate in {"anime", "shows", "movies"}:
                    directory_name = candidate
                    in_filters = False
                    current_filter = None
                    continue

            if directory_name in {"anime", "shows", "movies"}:
                if indent == 4 and stripped == "filters:":
                    in_filters = True
                    current_filter = None
                    continue
                if indent <= 2:
                    directory_name = None
                    in_filters = False
                    current_filter = None
                elif in_filters and indent == 6 and stripped.startswith("- "):
                    current_filter = {}
                    config["directories"][directory_name]["filters"].append(
                        current_filter
                    )
                    remainder = stripped[2:].strip()
                    if remainder and ":" in remainder:
                        key, value = remainder.split(":", 1)
                        current_filter[key.strip()] = value.strip()
                    continue
                elif (
                    in_filters
                    and indent >= 8
                    and current_filter is not None
                    and ":" in stripped
                ):
                    key, value = stripped.split(":", 1)
                    current_filter[key.strip()] = value.strip()
                    continue
                elif indent == 4 and ":" in stripped:
                    key, value = stripped.split(":", 1)
                    config["directories"][directory_name][key.strip()] = value.strip()
                    continue

        if indent == 0 and ":" in stripped:
            key, value = stripped.split(":", 1)
            config[key.strip()] = value.strip()

    return config


def zurg_to_buzz(zurg: dict) -> dict:
    anime_filters = zurg.get("directories", {}).get("anime", {}).get("filters", [])
    anime_patterns = []
    for item in anime_filters:
        if not isinstance(item, dict):
            continue
        for key in ("regex", "any_file_inside_regex"):
            value = item.get(key)
            if isinstance(value, str):
                anime_patterns.append(strip_regex_delimiters(value))
    unique_patterns = []
    for pattern in anime_patterns or [DEFAULT_ANIME_PATTERN]:
        if pattern not in unique_patterns:
            unique_patterns.append(pattern)

    hook = str(zurg.get("on_library_update", DEFAULT_HOOK)).strip()
    hook = hook.replace(' "$@"', "").strip()
    if not hook:
        hook = DEFAULT_HOOK

    buzz = {
        "provider": {"token": str(zurg.get("token", ""))},
        "poll_interval_secs": int(zurg.get("check_for_changes_every_secs", 10)),
        "server": {"bind": "0.0.0.0", "port": int(zurg.get("port", 9999))},
        "state_dir": "/app/data",
        "hooks": {"on_library_change": hook},
        "compat": {"enable_all_dir": True, "enable_unplayable_dir": True},
        "directories": {
            "anime": {"patterns": unique_patterns},
            "shows": {},
            "movies": {},
        },
        "request_timeout_secs": int(zurg.get("api_timeout_secs", 30)),
        "user_agent": "buzz/0.1",
        "version_label": "buzz/0.1",
    }
    return buzz


def strip_regex_delimiters(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith("/") and value.endswith("/"):
        return value[1:-1]
    return value


def parse_buzz_config(raw: str) -> dict:
    return yaml.safe_load(raw)


def buzz_to_zurg(buzz: dict) -> str:
    token = str(buzz.get("provider", {}).get("token", ""))
    poll = int(buzz.get("poll_interval_secs", 10))
    port = int(buzz.get("server", {}).get("port", 9999))
    hook = str(buzz.get("hooks", {}).get("on_library_change", DEFAULT_HOOK)).strip()
    anime_patterns = list(
        buzz.get("directories", {})
        .get("anime", {})
        .get("patterns", [DEFAULT_ANIME_PATTERN])
    )
    regex_lines = []
    for pattern in anime_patterns:
        regex = ensure_regex_delimiters(pattern)
        regex_lines.append(f"      - regex: {regex}")
    if not regex_lines:
        regex_lines.append(
            f"      - regex: {ensure_regex_delimiters(DEFAULT_ANIME_PATTERN)}"
        )

    return "\n".join(
        [
            "zurg: v1",
            f"token: {token}",
            '# host: "[::]"',
            f"# port: {port}",
            "# username:",
            "# password:",
            "# proxy:",
            "# concurrent_workers: 20",
            f"check_for_changes_every_secs: {poll}",
            "# repair_every_mins: 60",
            "# ignore_renames: false",
            "# retain_rd_torrent_name: false",
            "# retain_folder_name_extension: false",
            "enable_repair: true",
            "auto_delete_rar_torrents: true",
            f"# api_timeout_secs: {int(buzz.get('request_timeout_secs', 30))}",
            "# download_timeout_secs: 10",
            "# enable_download_mount: false",
            "# rate_limit_sleep_secs: 6",
            "# retries_until_failed: 2",
            "# network_buffer_size: 4194304 # 4MB",
            "# serve_from_rclone: false",
            "# verify_download_link: false",
            "# force_ipv6: false",
            f'on_library_update: {hook} "$@"',
            "",
            "directories:",
            "  anime:",
            "    group_order: 10",
            "    group: media",
            "    filters:",
            *regex_lines,
            "  shows:",
            "    group_order: 20",
            "    group: media",
            "    filters:",
            "      - has_episodes: true",
            "  movies:",
            "    group_order: 30",
            "    group: media",
            "    only_show_the_biggest_file: true",
            "    filters:",
            "      - regex: /.*/",
            "",
        ]
    )


def ensure_regex_delimiters(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("/") and stripped.endswith("/"):
        return stripped
    return f"/{stripped}/"


def convert(source_format: str, target_format: str, raw: str) -> str:
    if source_format == "zurg" and target_format == "buzz":
        return yaml.safe_dump(zurg_to_buzz(parse_zurg_config(raw)), sort_keys=False)
    if source_format == "buzz" and target_format == "zurg":
        return buzz_to_zurg(parse_buzz_config(raw))
    raise ValueError(f"Unsupported conversion: {source_format} -> {target_format}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert config files between Zurg and Buzz."
    )
    parser.add_argument(
        "--from", dest="source_format", choices=["zurg", "buzz"], required=True
    )
    parser.add_argument(
        "--to", dest="target_format", choices=["zurg", "buzz"], required=True
    )
    parser.add_argument("input", help="Input config path")
    parser.add_argument(
        "-o", "--output", help="Write output to this path instead of stdout"
    )
    args = parser.parse_args(argv)

    raw = Path(args.input).read_text(encoding="utf-8")
    converted = convert(args.source_format, args.target_format, raw)
    if args.output:
        Path(args.output).write_text(converted, encoding="utf-8")
    else:
        sys.stdout.write(converted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
