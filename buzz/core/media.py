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


def parse_movie(stem: str, *, folder: str = "") -> dict[str, Any] | None:
    if re.search(r"(?i)\bS\d{1,2}E\d{1,2}\b", stem):
        return None

    # Try parsing the file stem first
    result = _try_extract_movie(stem)
    if result:
        return result

    # Fallback to torrent folder name if provided
    if folder:
        folder_result = _try_extract_movie(folder)
        if folder_result:
            # If folder has year but stem doesn't, use folder's year
            # but stem's cleaned name (usually more accurate for the movie itself)
            # unless the stem is very short (like "2001") or noisy
            cleaned_stem = NOISE_RE.sub("", canonical_spaces(
                stem.replace(".", " ").replace("_", " ").replace("-", " ")
            )).strip()
            
            # Remove trailing junk from stem (like opening parens/brackets)
            cleaned_stem = re.sub(r"[\(\[\s-]+$", "", cleaned_stem)
            
            title = sanitize_path_component(pretty_title(cleaned_stem))
            # If stem title is too short (likely just a year or part of it), 
            # or sanitize/pretty_title made it empty, use folder's title
            if title and len(title) > 4:
                return {"title": title, "year": folder_result["year"]}
            
            # If stem cleanup failed or title too short, fall back to folder's title
            return folder_result

    return None


def _try_extract_movie(text: str) -> dict[str, Any] | None:
    cleaned = canonical_spaces(
        text.replace(".", " ").replace("_", " ").replace("-", " ")
    )
    
    # Find all year matches and use the LAST one
    matches = list(YEAR_RE.finditer(cleaned))
    if not matches:
        return None
        
    match = matches[-1]
    title_part = cleaned[: match.start()]
    
    # Remove noise and trailing junk before the year (like " (", " [", " -")
    title_part = NOISE_RE.sub("", title_part)
    title_part = re.sub(r"[\(\[\s-]+$", "", title_part)
    
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
