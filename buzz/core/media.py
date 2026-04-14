import re
from pathlib import PurePath, PurePosixPath
from typing import Any

from .constants import (
    NOISE_RE,
    SHOW_PATTERNS,
    SIDECAR_EXTENSIONS,
    VIDEO_EXTENSIONS,
    YEAR_RE,
)
from .utils import canonical_spaces, pretty_title, sanitize_path_component


def is_video_file(path: str | PurePath) -> bool:
    if isinstance(path, str):
        suffix = PurePosixPath(path).suffix.lower()
    else:
        suffix = path.suffix.lower()
    return suffix in VIDEO_EXTENSIONS


def is_sidecar_file(path: str | PurePath) -> bool:
    if isinstance(path, str):
        suffix = PurePosixPath(path).suffix.lower()
    else:
        suffix = path.suffix.lower()
    return suffix in SIDECAR_EXTENSIONS


def is_probably_media_content_type(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.split(";", 1)[0].strip().lower()
    if normalized.startswith(("video/", "audio/", "application/octet-stream")):
        return True
    if normalized in {
        "application/mp4",
        "application/vnd.apple.mpegurl",
        "application/force-download",
        "application/download",
        "binary/octet-stream",
    }:
        return True
    return False


def looks_like_markup(payload: bytes) -> bool:
    head = payload.lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"<?xml", b"{", b"["))


def parse_movie(stem: str) -> dict[str, Any] | None:
    if re.search(r"(?i)\bS\d{1,2}E\d{1,2}\b", stem):
        return None
    cleaned = canonical_spaces(
        stem.replace(".", " ").replace("_", " ").replace("-", " ")
    )
    match = YEAR_RE.search(cleaned)
    if not match:
        return None
    title_part = cleaned[: match.start()]
    title_part = NOISE_RE.sub("", title_part)
    title = sanitize_path_component(pretty_title(title_part))
    if not title:
        return None
    return {"title": title, "year": int(match.group(1))}


def parse_show(stem: str) -> dict[str, Any] | None:
    cleaned = canonical_spaces(
        stem.replace(".", " ").replace("_", " ").replace("-", " ")
    )
    for pattern in SHOW_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        series = cleaned[: match.start()]
        series = NOISE_RE.sub("", series)
        series = sanitize_path_component(pretty_title(series))
        if not series:
            return None
        return {
            "series": series,
            "season": int(match.group("season")),
            "episode": int(match.group("episode")),
        }
    return None
