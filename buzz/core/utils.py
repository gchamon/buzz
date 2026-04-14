import json
import posixpath
import re
import time
from datetime import UTC, datetime
from email.utils import formatdate
from typing import Any
from xml.sax.saxutils import escape


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_date(value: str | None) -> str:
    if value:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return formatdate(timestamp, usegmt=True)
        except ValueError:
            pass
    return formatdate(time.time(), usegmt=True)


def html_escape(value: Any) -> str:
    return escape(str(value), {"'": "&#x27;", '"': "&quot;"})


def format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except TypeError, ValueError:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def normalize_posix_path(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned == "/":
        return ""
    normalized = posixpath.normpath("/" + cleaned.lstrip("/"))
    if normalized == "/":
        return ""
    return normalized.lstrip("/")


def split_path(value: str) -> tuple[str, ...]:
    normalized = normalize_posix_path(value)
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part)


def strip_regex_delimiters(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith("/") and value.endswith("/"):
        return value[1:-1]
    return value


def ensure_regex_delimiters(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("/") and stripped.endswith("/"):
        return stripped
    return f"/{stripped}/"


def canonical_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .-_")


def sanitize_path_component(value: str) -> str:
    value = value.replace("/", " ").replace("\\", " ")
    return canonical_spaces(value)


def pretty_title(raw: str) -> str:
    cleaned = canonical_spaces(raw.replace(".", " ").replace("_", " "))
    words = []
    for word in cleaned.split():
        if word.isupper() and len(word) <= 4:
            words.append(word)
        elif re.fullmatch(r"[ivxlcdm]+", word, re.IGNORECASE):
            words.append(word.upper())
        else:
            words.append(word.capitalize())
    return " ".join(words)
