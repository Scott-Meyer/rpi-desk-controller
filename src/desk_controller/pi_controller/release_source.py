"""Acquire a verified Pi source release into a new, isolated staging directory.

GitHub's tag ref, rather than a release's target_commitish, identifies the
source. This module never installs it or modifies a running checkout.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests
from packaging.version import InvalidVersion, Version

API = "https://api.github.com/repos/Scott-Meyer/rpi-desk-controller"
RELEASES = "https://github.com/Scott-Meyer/rpi-desk-controller/releases/tag/"
ARCHIVE_NAME = "DeskController-Pi-source.tar.gz"
MANIFEST_NAME = "DeskController-Pi-source.json"
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.]+)?\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_METADATA = 128 * 1024
MAX_TREE_METADATA = 4 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_MANIFEST = 16 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 5000
MAX_TAG_DEPTH = 8
_REQUIRED = {
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "requirements.txt",
    "config/config.example.yaml",
    "src/desk_controller/__init__.py",
    "src/desk_controller/pi_controller/main.py",
}
_ROOT_FILES = _REQUIRED - {
    "config/config.example.yaml",
    "src/desk_controller/__init__.py",
    "src/desk_controller/pi_controller/main.py",
}
_SOURCE_DIRS = {"scripts", "src", "systemd"}
_FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "site-packages",
    "build",
    "dist",
    "config.yaml",
    "_source_version.json",
}


class ReleaseSourceError(Exception):
    """The release source could not be acquired or verified."""


class UnsupportedRelease(ReleaseSourceError):
    """The published release does not offer an installable Pi source."""


class ReleaseIntegrityError(ReleaseSourceError):
    """Published metadata or source content failed verification."""


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str
    release_id: int
    archive_asset_id: int
    manifest_asset_id: int
    release_url: str
    version: str


@dataclass(frozen=True)
class StagedRelease:
    release: Release
    destination: Path


def _download(url: str, limit: int, output=None, *, binary=False) -> bytes | None:
    """Read a bounded GitHub API response, optionally streaming to a file."""
    try:
        response = requests.get(
            url,
            headers={
                "Accept": "application/octet-stream"
                if binary
                else "application/vnd.github+json"
            },
            timeout=(5, 30),
            stream=True,
        )
        try:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > limit:
                raise ReleaseSourceError("GitHub response exceeds size limit")
            chunks = []
            received = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                received += len(chunk)
                if received > limit:
                    raise ReleaseSourceError("GitHub response exceeds size limit")
                if output is None:
                    chunks.append(chunk)
                else:
                    output.write(chunk)
            return b"".join(chunks) if output is None else None
        finally:
            response.close()
    except (requests.RequestException, OSError, ValueError) as exc:
        raise ReleaseSourceError("GitHub download failed") from exc


def _get_json(url: str, limit: int = MAX_METADATA) -> dict:
    raw = _download(url, limit)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ReleaseSourceError("GitHub returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ReleaseSourceError("GitHub returned invalid metadata")
    return data


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _release_metadata(data: dict) -> tuple[str, int, int, int, str]:
    tag = data.get("tag_name")
    if data.get("draft") or data.get("prerelease"):
        raise UnsupportedRelease("draft or prerelease")
    if not isinstance(tag, str) or len(tag) > 80 or not _TAG.fullmatch(tag):
        raise UnsupportedRelease("invalid release version tag")
    try:
        version = str(Version(tag[1:]))
    except InvalidVersion as exc:
        raise UnsupportedRelease("invalid release version tag") from exc
    release_id = data.get("id")
    if not _positive_id(release_id):
        raise ReleaseSourceError("invalid GitHub release ID")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ReleaseSourceError("invalid GitHub release assets")
    ids = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseSourceError("invalid GitHub asset")
        name = asset.get("name")
        if name in (ARCHIVE_NAME, MANIFEST_NAME):
            asset_id = asset.get("id")
            if name in ids or not _positive_id(asset_id):
                raise ReleaseSourceError("duplicate or invalid Pi source asset")
            ids[name] = asset_id
    if ARCHIVE_NAME not in ids or MANIFEST_NAME not in ids:
        raise UnsupportedRelease("Pi source assets are missing")
    return tag, release_id, ids[ARCHIVE_NAME], ids[MANIFEST_NAME], version


def _tag_commit(tag: str) -> str:
    # A lightweight ref points to a commit; an annotated ref points to a tag
    # object, which can itself point to another tag object.
    ref = _get_json(f"{API}/git/ref/tags/{quote(tag, safe='')}")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise ReleaseIntegrityError("GitHub returned a different tag ref")
    obj = ref.get("object")
    visited = set()
    for _ in range(MAX_TAG_DEPTH):
        if not isinstance(obj, dict):
            break
        sha, kind = obj.get("sha"), obj.get("type")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha) or sha in visited:
            break
        if kind == "commit":
            return sha
        if kind != "tag":
            break
        visited.add(sha)
        annotation = _get_json(f"{API}/git/tags/{sha}")
        if annotation.get("sha") != sha:
            break
        obj = annotation.get("object")
    raise ReleaseIntegrityError("tag does not peel to a commit")


def _tag_source_tree(commit: str) -> dict[str, tuple[str, str]]:
    """Get the exact paths, blob IDs and modes selected by the Pi archive job.

    The release assets are not an authority for source identity. The peeled
    tag's immutable Git commit/tree objects are, including executable bits.
    """
    commit_data = _get_json(f"{API}/git/commits/{commit}")
    root = commit_data.get("tree")
    if (
        commit_data.get("sha") != commit
        or not isinstance(root, dict)
        or not isinstance(root.get("sha"), str)
        or not _SHA.fullmatch(root["sha"])
    ):
        raise ReleaseIntegrityError("invalid tagged commit tree")
    tree_sha = root["sha"]
    tree_data = _get_json(f"{API}/git/trees/{tree_sha}?recursive=1", MAX_TREE_METADATA)
    entries = tree_data.get("tree")
    if (
        tree_data.get("sha") != tree_sha
        or tree_data.get("truncated") is not False
        or not isinstance(entries, list)
        or len(entries) > MAX_TREE_ENTRIES
    ):
        raise ReleaseIntegrityError("incomplete or invalid tagged source tree")

    selected = {}
    kinds = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReleaseIntegrityError("invalid tagged source tree entry")
        path, kind, mode, sha = (
            entry.get("path"),
            entry.get("type"),
            entry.get("mode"),
            entry.get("sha"),
        )
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or path in kinds
            or not isinstance(sha, str)
            or not _SHA.fullmatch(sha)
            or (kind, mode)
            not in {
                ("tree", "040000"),
                ("blob", "100644"),
                ("blob", "100755"),
                ("blob", "120000"),
                ("commit", "160000"),
            }
            or (
                kind == "blob"
                and (type(entry.get("size")) is not int or entry["size"] < 0)
            )
        ):
            raise ReleaseIntegrityError("invalid tagged source tree entry")
        kinds[path] = kind
        if kind == "tree":
            continue
        if (
            path in _ROOT_FILES
            or path == "config/config.example.yaml"
            or any(path.startswith(f"{directory}/") for directory in _SOURCE_DIRS)
        ):
            if kind != "blob" or mode not in ("100644", "100755"):
                raise ReleaseIntegrityError("tag contains non-regular Pi source")
            _allowed_path(path, False)
            selected[path] = (sha, mode)

    if any(
        kinds.get(path.rsplit("/", 1)[0]) != "tree" for path in kinds if "/" in path
    ):
        raise ReleaseIntegrityError("tagged source tree is missing a parent directory")
    if not _REQUIRED.issubset(selected):
        raise ReleaseIntegrityError("tag is missing required Pi source")
    return selected


def _verify_source_tree(
    destination: Path, file_modes: dict[str, bool], commit: str
) -> None:
    expected = _tag_source_tree(commit)
    if file_modes.keys() != expected.keys():
        raise ReleaseIntegrityError("Pi source file set differs from tagged commit")
    for name, (blob_sha, mode) in expected.items():
        path = destination / name
        stat = path.stat()
        # Compare archive modes, not the extracted filesystem's st_mode:
        # Windows runners do not preserve POSIX executable bits in stat().
        if file_modes[name] != (mode == "100755"):
            raise ReleaseIntegrityError(f"Pi source mode differs from tag: {name}")
        # Git's object IDs are SHA-1 in this repository; use its exact blob format.
        digest = hashlib.sha1(f"blob {stat.st_size}\0".encode())  # nosec B324
        with path.open("rb") as source:
            for block in iter(lambda: source.read(64 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != blob_sha:
            raise ReleaseIntegrityError(f"Pi source content differs from tag: {name}")


def _identity(data: dict) -> Release:
    tag, release_id, archive_id, manifest_id, version = _release_metadata(data)
    return Release(
        tag=tag,
        commit=_tag_commit(tag),
        release_id=release_id,
        archive_asset_id=archive_id,
        manifest_asset_id=manifest_id,
        release_url=f"{RELEASES}{quote(tag, safe='')}",
        version=version,
    )


def discover_latest() -> Release:
    """Resolve the latest stable GitHub release and its peeled tag commit.

    Raises UnsupportedRelease for releases lacking installable assets, or
    ReleaseSourceError for unavailable/invalid GitHub metadata.
    """
    return _identity(_get_json(f"{API}/releases/latest"))


def _verify_manifest(raw: bytes, release: Release, archive: Path) -> None:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest field")
            result[key] = value
        return result

    try:
        manifest = json.loads(raw, object_pairs_hook=unique_keys)
    except (ValueError, TypeError) as exc:
        raise ReleaseIntegrityError("invalid Pi source manifest") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "tag", "commit", "archive_sha256"}
        or type(manifest["schema"]) is not int
        or manifest["schema"] != 1
        or manifest["tag"] != release.tag
        or manifest["commit"] != release.commit
        or not isinstance(manifest["archive_sha256"], str)
        or not _DIGEST.fullmatch(manifest["archive_sha256"])
    ):
        raise ReleaseIntegrityError("Pi source manifest differs from peeled tag")
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(64 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != manifest["archive_sha256"]:
        raise ReleaseIntegrityError("Pi source archive checksum differs from manifest")


def _allowed_path(name: str, is_dir: bool) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise ReleaseIntegrityError("unsafe archive path")
    clean = name[:-1] if is_dir and name.endswith("/") else name
    parts = clean.split("/")
    if any(
        part in ("", ".", "..") or part.startswith(".") or part in _FORBIDDEN_PARTS
        for part in parts
    ):
        raise ReleaseIntegrityError("unsafe archive path")
    if is_dir:
        allowed = parts[0] in _SOURCE_DIRS or parts == ["config"]
        if parts[0] == "config" and len(parts) != 1:
            allowed = False
    else:
        allowed = (
            clean in _ROOT_FILES
            or clean == "config/config.example.yaml"
            or (parts[0] in _SOURCE_DIRS and len(parts) > 1)
        )
    if not allowed:
        raise ReleaseIntegrityError("unexpected release archive path")
    return tuple(parts)


class _LimitedReader:
    def __init__(self, source, limit: int):
        self.source = source
        self.remaining = limit

    def read(self, size=-1):
        if size < 0:
            raise ReleaseIntegrityError("unbounded archive read")
        # Read one beyond the allowance to reject oversized expanded streams.
        chunk = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise ReleaseIntegrityError("expanded archive exceeds size limit")
        return chunk


def _extract(archive: Path, destination: Path) -> dict[str, bool]:
    seen = set()
    files = {}
    total = 0
    try:
        with (
            archive.open("rb") as source,
            gzip.GzipFile(fileobj=source) as decompressed,
        ):
            limited = _LimitedReader(decompressed, MAX_EXPANDED)
            with tarfile.open(fileobj=limited, mode="r|") as tar:
                for member in tar:
                    if len(seen) >= MAX_FILES:
                        raise ReleaseIntegrityError("too many source archive entries")
                    if not member.isfile() and not member.isdir():
                        raise ReleaseIntegrityError(
                            "release archive contains non-regular entry"
                        )
                    parts = _allowed_path(member.name, member.isdir())
                    if parts in seen:
                        raise ReleaseIntegrityError("duplicate archive path")
                    seen.add(parts)
                    if member.isdir():
                        (destination.joinpath(*parts)).mkdir(
                            parents=True, exist_ok=True
                        )
                        continue
                    total += member.size
                    if member.size > MAX_EXPANDED or total > MAX_EXPANDED:
                        raise ReleaseIntegrityError(
                            "expanded source files exceed size limit"
                        )
                    path = destination.joinpath(*parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as entry, path.open("xb") as target:
                        shutil.copyfileobj(entry, target, length=64 * 1024)
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)
                    files["/".join(parts)] = bool(member.mode & 0o111)
        if not _REQUIRED.issubset(files):
            raise ReleaseIntegrityError("release archive is missing required Pi source")
        return files
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, OSError) as exc:
        raise ReleaseIntegrityError("invalid Pi source archive") from exc


def stage_release(release: Release, destination: Path | str) -> StagedRelease:
    """Verify and extract into a *new* directory; never update live files.

    The caller owns installation and choosing an isolated destination. An
    existing destination is refused, even when verification would succeed.
    """
    if not isinstance(release, Release):
        raise TypeError("release must be a discovered Release")
    if not _positive_id(release.release_id):
        raise ReleaseSourceError("invalid release identity")
    destination = Path(destination).absolute()
    if (
        not destination.parent.is_dir()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ReleaseSourceError(
            "staging destination must be new with an existing parent"
        )
    current = _identity(_get_json(f"{API}/releases/{release.release_id}"))
    if current != release:
        raise ReleaseIntegrityError("release or tag changed since discovery")
    try:
        with tempfile.TemporaryDirectory(
            prefix=".pi-source-", dir=destination.parent
        ) as temp:
            staged = Path(temp) / "source"
            staged.mkdir()
            archive = Path(temp) / ARCHIVE_NAME
            manifest = _download(
                f"{API}/releases/assets/{release.manifest_asset_id}",
                MAX_MANIFEST,
                binary=True,
            )
            with archive.open("xb") as output:
                _download(
                    f"{API}/releases/assets/{release.archive_asset_id}",
                    MAX_ARCHIVE,
                    output,
                    binary=True,
                )
            _verify_manifest(manifest, release, archive)
            files = _extract(archive, staged)
            _verify_source_tree(staged, files, release.commit)
            # Only verified source is promoted; the temporary download and
            # partial extraction are removed automatically on any failure.
            staged.chmod(0o755)
            if destination.exists() or destination.is_symlink():
                raise ReleaseSourceError("staging destination was created concurrently")
            os.rename(staged, destination)
    except OSError as exc:
        raise ReleaseSourceError("could not stage Pi source") from exc
    return StagedRelease(release=release, destination=destination)
