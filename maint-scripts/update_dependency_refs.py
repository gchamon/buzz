#!/usr/bin/env python3
"""Update container image digest pins in repository-maintained files."""

from __future__ import annotations

import concurrent.futures
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


MAX_PARALLEL_RESOLVERS = 4
PROJECT_IMAGE_PREFIX = "registry.gitlab.com/gabriel.chamon/buzz/buzz:"
TARGETS = (
    Path("buzz/Dockerfile"),
    Path("docker-compose.yml"),
    Path(".gitlab/ci/gitleaks.gitlab-ci.yml"),
    Path(".gitlab/ci/test.gitlab-ci.yml"),
    Path(".gitlab/ci/security.gitlab-ci.yml"),
)

DIGEST_CHAIN_RE = re.compile(r"(?:@sha256:[a-fA-F0-9]+)+$")
FROM_RE = re.compile(r"^(\s*FROM\s+)(\S+)(.*)$")
IMAGE_RE = re.compile(r"^(\s*(?:image|name):\s+)([^\s#]+)(.*)$")


@dataclass(frozen=True)
class ImageRef:
    """A discovered image reference that should be pinned."""

    file: Path
    current: str
    inspect_ref: str


@dataclass(frozen=True)
class DigestResolutionError:
    """A failed digest resolution with enough context to fix the source ref."""

    image: ImageRef
    error: str


def strip_digest(ref: str) -> str:
    """Return *ref* without any trailing sha256 digest chain."""
    return DIGEST_CHAIN_RE.sub("", ref)


def normalize_for_inspect(ref: str) -> str:
    """Return a skopeo-compatible image ref for Docker Hub shorthand."""
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return ref
    if "/" not in ref:
        return f"docker.io/library/{ref}"
    return f"docker.io/{ref}"


def unquote_ref(ref: str) -> str:
    """Strip simple YAML quotes around an image ref."""
    return ref.strip("'\"")


def discover_images(targets: tuple[Path, ...]) -> list[ImageRef]:
    """Discover image refs from known maintenance target files."""
    images: list[ImageRef] = []
    seen: set[tuple[Path, str]] = set()

    for path in targets:
        for line in path.read_text().splitlines():
            match = FROM_RE.match(line) or IMAGE_RE.match(line)
            if match is None:
                continue

            current = strip_digest(unquote_ref(match.group(2)))
            if current.startswith(PROJECT_IMAGE_PREFIX):
                continue

            key = (path, current)
            if key in seen:
                continue
            seen.add(key)
            images.append(
                ImageRef(
                    file=path,
                    current=current,
                    inspect_ref=normalize_for_inspect(current),
                )
            )

    return images


def fetch_digest(image: ImageRef) -> str:
    """Resolve the current digest for *image*."""
    print(f"resolving {image.current}...", flush=True)
    result = subprocess.run(
        [
            "skopeo",
            "inspect",
            "--format",
            "{{.Digest}}",
            f"docker://{image.inspect_ref}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    digest = result.stdout.strip()
    print(f" done resolving {image.current}", flush=True)
    return digest


def resolve_digests(images: list[ImageRef]) -> dict[ImageRef, str]:
    """Resolve image digests with bounded concurrency."""
    digests: dict[ImageRef, str] = {}
    failures: list[DigestResolutionError] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_PARALLEL_RESOLVERS
    ) as executor:
        future_to_image = {
            executor.submit(fetch_digest, image): image for image in images
        }
        for future in concurrent.futures.as_completed(future_to_image):
            image = future_to_image[future]
            try:
                digests[image] = future.result()
            except subprocess.CalledProcessError as exc:
                failures.append(
                    DigestResolutionError(
                        image=image,
                        error=(exc.stderr or str(exc)).strip(),
                    )
                )
    if failures:
        raise RuntimeError(format_resolution_failures(failures))
    return digests


def format_resolution_failures(
    failures: list[DigestResolutionError],
) -> str:
    """Build a readable error message for failed image lookups."""
    lines = ["failed to resolve one or more dependency digests:"]
    for failure in failures:
        lines.extend(
            [
                f"- {failure.image.file}: {failure.image.current}",
                f"  inspected as: docker://{failure.image.inspect_ref}",
                f"  error: {failure.error}",
            ]
        )
    return "\n".join(lines)


def pin_line(line: str, image: ImageRef, digest: str) -> str:
    """Pin *image* in one line if the line contains that image declaration."""
    newline = ""
    body = line
    if body.endswith("\n"):
        newline = "\n"
        body = body[:-1]
        if body.endswith("\r"):
            newline = "\r\n"
            body = body[:-1]

    match = FROM_RE.match(body) or IMAGE_RE.match(body)
    if match is None:
        return line

    ref = unquote_ref(match.group(2))
    if strip_digest(ref) != image.current:
        return line

    return f"{match.group(1)}{image.current}@{digest}{match.group(3)}{newline}"


def apply_digest(image: ImageRef, digest: str) -> None:
    """Apply a resolved digest pin to all matching refs in image.file."""
    print(f"updating {image.current} pin at {image.file}...")
    lines = image.file.read_text().splitlines(keepends=True)
    updated = [pin_line(line, image, digest) for line in lines]
    image.file.write_text("".join(updated))


def validate() -> None:
    """Run repository validation checks for changed dependency references."""
    subprocess.run(
        ["docker", "compose", "config"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "python",
            "-c",
            "import yaml, sys; "
            "[yaml.safe_load(open(path)) for path in sys.argv[1:]]",
            ".gitlab-ci.yml",
            *[str(path) for path in Path(".gitlab/ci").glob("*.gitlab-ci.yml")],
        ],
        check=True,
    )


def main() -> int:
    """Update dependency digest pins."""
    images = discover_images(TARGETS)
    try:
        digests = resolve_digests(images)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    for image in images:
        apply_digest(image, digests[image])

    validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
