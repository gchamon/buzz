import re

VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}

SIDECAR_EXTENSIONS = {
    ".ass",
    ".idx",
    ".nfo",
    ".smi",
    ".srt",
    ".ssa",
    ".sub",
    ".sup",
    ".txt",
    ".vtt",
}

SHOW_PATTERNS = (
    re.compile(r"(?i)\bS(?P<season>\d{1,2})E(?P<episode>\d{1,2})\b"),
    re.compile(r"(?i)\b(?P<season>\d{1,2})x(?P<episode>\d{1,2})\b"),
)

DEFAULT_ANIME_PATTERN = r"\b[a-fA-F0-9]{8}\b"

NOISE_RE = re.compile(
    r"(?i)\b("
    r"1080p|2160p|720p|480p|4k|bluray|brrip|bdrip|dvdrip|dvd|webrip|web[- ]?dl|"
    r"hdr|hdr10|remux|proper|repack|extended|unrated|criterion|x264|x265|h\.?264|"
    r"h\.?265|hevc|av1|aac|ac3|dts|truehd|atmos|yts|rarbg|amzn|nf|dsnp|hmax"
    r")\b"
)

YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
