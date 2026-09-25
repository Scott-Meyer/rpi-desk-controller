"""A release is staged only when GitHub's tag, manifest, and archive agree."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_controller.pi_controller import release_source as source

COMMIT = "a" * 40
ANNOTATION = "b" * 40
TREE = "c" * 40
TAG = "v1.2.3"
REQUIRED = {
    "README.md": b"hello",
    "LICENSE": b"license",
    "THIRD_PARTY_NOTICES.md": b"notices",
    "pyproject.toml": b"[build-system]",
    "requirements.txt": b"requests",
    "config/config.example.yaml": b"example: true",
    "src/desk_controller/__init__.py": b"__version__ = '1.2.3'",
    "src/desk_controller/pi_controller/main.py": b"def main(): pass",
    "scripts/setup_rpi.sh": b"#!/bin/sh\n",
    "systemd/controller.service": b"[Unit]",
}


def git_blob(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def archive_with(extra=None, *, omit=(), executable=("scripts/setup_rpi.sh",)):
    entries = {**REQUIRED, **(extra or {})}
    for name in omit:
        del entries[name]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, content in entries.items():
            item = tarfile.TarInfo(name)
            item.mode = 0o755 if name in executable else 0o644
            if isinstance(content, tuple):
                item.type = content[0]
                item.linkname = content[1]
                item.size = 0
            else:
                item.size = len(content)
            tar.addfile(
                item, io.BytesIO(content) if isinstance(content, bytes) else None
            )
    return output.getvalue()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]

    def close(self):
        pass


class GitHub:
    def __init__(self, archive=None, manifest=None):
        self.archive = archive if archive is not None else archive_with()
        self.manifest = (
            manifest
            if manifest is not None
            else {
                "schema": 1,
                "tag": TAG,
                "commit": COMMIT,
                "archive_sha256": hashlib.sha256(self.archive).hexdigest(),
            }
        )
        self.release = {
            "tag_name": TAG,
            "target_commitish": "main",  # Must never be used as tag identity.
            "html_url": "https://attacker.invalid/releases/fake",
            "id": 23,
            "assets": [
                {
                    "name": source.ARCHIVE_NAME,
                    "id": 41,
                    "browser_download_url": "https://attacker.invalid/archive",
                },
                {"name": source.MANIFEST_NAME, "id": 42},
            ],
        }
        self.ref = {
            "ref": f"refs/tags/{TAG}",
            "object": {"type": "tag", "sha": ANNOTATION},
        }
        self.annotation = {
            "sha": ANNOTATION,
            "object": {"type": "commit", "sha": COMMIT},
        }
        self.commit = {"sha": COMMIT, "tree": {"sha": TREE}}
        paths = set(REQUIRED)
        directories = {
            "/".join(name.split("/")[:depth])
            for name in paths
            for depth in range(1, len(name.split("/")))
        }
        self.tree = {
            "sha": TREE,
            "truncated": False,
            "tree": [
                {
                    "path": directory,
                    "mode": "040000",
                    "type": "tree",
                    "sha": "d" * 40,
                }
                for directory in sorted(directories)
            ]
            + [
                {
                    "path": name,
                    "mode": "100755" if name == "scripts/setup_rpi.sh" else "100644",
                    "type": "blob",
                    "sha": git_blob(data),
                    "size": len(data),
                }
                for name, data in REQUIRED.items()
            ]
            + [
                {
                    "path": "RELEASING.md",
                    "mode": "100644",
                    "type": "blob",
                    "sha": git_blob(b"not selected by workflow"),
                    "size": 24,
                }
            ],
        }
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payloads = {
            f"{source.API}/releases/latest": self.release,
            f"{source.API}/releases/23": self.release,
            f"{source.API}/git/ref/tags/{TAG}": self.ref,
            f"{source.API}/git/tags/{ANNOTATION}": self.annotation,
            f"{source.API}/git/commits/{COMMIT}": self.commit,
            f"{source.API}/git/trees/{TREE}?recursive=1": self.tree,
            f"{source.API}/releases/assets/41": self.archive,
            f"{source.API}/releases/assets/42": self.manifest,
        }
        payload = payloads[url]
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        return FakeResponse(payload)


class ReleaseSourceTests(unittest.TestCase):
    def test_annotated_tag_and_verified_source_are_staged_without_installing(self):
        github = GitHub()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(source.requests, "get", github.get),
        ):
            release = source.discover_latest()
            self.assertEqual(release.commit, COMMIT)
            self.assertEqual(release.version, "1.2.3")
            self.assertEqual(release.release_url, f"{source.RELEASES}{TAG}")
            result = source.stage_release(release, Path(tmp) / "new-source")
            self.assertEqual(result.release, release)
            self.assertEqual(
                (result.destination / "scripts/setup_rpi.sh").read_bytes(),
                b"#!/bin/sh\n",
            )
            self.assertEqual(
                (result.destination / "config/config.example.yaml").read_bytes(),
                b"example: true",
            )
            self.assertTrue(
                (result.destination / "scripts/setup_rpi.sh").stat().st_mode & 0o111
            )
            self.assertFalse((result.destination / "RELEASING.md").exists())
            self.assertIn(
                f"{source.API}/git/trees/{TREE}?recursive=1",
                [call[0] for call in github.calls],
            )
            self.assertFalse((result.destination / source.ARCHIVE_NAME).exists())
            self.assertEqual(
                sorted(p.name for p in Path(tmp).iterdir()), ["new-source"]
            )
            self.assertTrue(
                all(call[0].startswith(source.API) for call in github.calls)
            )
            self.assertTrue(
                all(
                    call[1]["headers"]["Accept"] == "application/octet-stream"
                    for call in github.calls
                    if "/assets/" in call[0]
                )
            )

    def test_missing_assets_are_unsupported_and_existing_source_is_never_replaced(self):
        github = GitHub()
        with patch.object(source.requests, "get", github.get):
            github.release["assets"] = []
            with self.assertRaises(source.UnsupportedRelease):
                source.discover_latest()
            github.release["assets"] = [
                {"name": source.ARCHIVE_NAME, "id": 41},
                {"name": source.MANIFEST_NAME, "id": 42},
            ]
            release = source.discover_latest()
            with tempfile.TemporaryDirectory() as tmp:
                destination = Path(tmp) / "running"
                destination.mkdir()
                (destination / "config.yaml").write_text("keep this")
                with self.assertRaises(source.ReleaseSourceError):
                    source.stage_release(release, destination)
                self.assertEqual((destination / "config.yaml").read_text(), "keep this")
                self.assertEqual(
                    sorted(p.name for p in Path(tmp).iterdir()), ["running"]
                )

    def test_changed_tag_or_manifest_fails_closed_without_staged_files(self):
        github = GitHub()
        with (
            patch.object(source.requests, "get", github.get),
            tempfile.TemporaryDirectory() as tmp,
        ):
            release = source.discover_latest()
            github.annotation["object"]["sha"] = "c" * 40
            with self.assertRaises(source.ReleaseIntegrityError):
                source.stage_release(release, Path(tmp) / "source")
            github.annotation["object"]["sha"] = COMMIT
            original_manifest = dict(github.manifest)
            for field, value in (
                ("schema", 2),
                ("tag", "v9.9.9"),
                ("commit", "c" * 40),
                ("archive_sha256", "0" * 64),
            ):
                with self.subTest(field=field):
                    github.manifest = {**original_manifest, field: value}
                    with self.assertRaises(source.ReleaseIntegrityError):
                        source.stage_release(release, Path(tmp) / "source")
                    self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_changed_archive_and_recomputed_manifest_cannot_impersonate_tag(self):
        # Release-manager control of both assets is insufficient: the Git tree
        # is fixed independently by the peeled tag, including executable bits.
        attempts = [
            archive_with({"src/desk_controller/pi_controller/main.py": b"evil code"}),
            archive_with({"scripts/new_install.sh": b"#!/bin/sh\nevil\n"}),
            archive_with(omit=("systemd/controller.service",)),
            archive_with(executable=()),
            archive_with(executable=("scripts/setup_rpi.sh", "requirements.txt")),
        ]
        for archive in attempts:
            with self.subTest(archive_sha256=hashlib.sha256(archive).hexdigest()):
                github = GitHub(archive=archive)
                with (
                    patch.object(source.requests, "get", github.get),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    release = source.discover_latest()
                    with self.assertRaises(source.ReleaseIntegrityError):
                        source.stage_release(release, Path(tmp) / "new-source")
                    self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_incomplete_or_malformed_tag_tree_is_never_promoted(self):
        def missing_blob(github):
            github.tree["tree"] = [
                entry
                for entry in github.tree["tree"]
                if entry["path"] != "systemd/controller.service"
            ]

        def extra_blob(github):
            github.tree["tree"].append(
                {
                    "path": "src/new.py",
                    "type": "blob",
                    "mode": "100644",
                    "sha": git_blob(b"from the tag"),
                    "size": 12,
                }
            )

        def symlink(github):
            next(
                item
                for item in github.tree["tree"]
                if item["path"] == "systemd/controller.service"
            )["mode"] = "120000"

        def missing_parent(github):
            github.tree["tree"] = [
                item for item in github.tree["tree"] if item["path"] != "scripts"
            ]

        def bad_size(github):
            next(item for item in github.tree["tree"] if item["path"] == "README.md")[
                "size"
            ] = "unknown"

        attacks = (
            lambda github: github.commit.update(sha="f" * 40),
            lambda github: github.commit["tree"].update(sha="not a SHA"),
            lambda github: github.tree.update(sha="f" * 40),
            lambda github: github.tree.update(truncated=True),
            lambda github: github.tree.pop("truncated"),
            lambda github: github.tree.update(tree={}),
            missing_blob,
            missing_parent,
            extra_blob,
            symlink,
            bad_size,
            lambda github: github.tree["tree"].append(github.tree["tree"][-1].copy()),
        )
        for mutate in attacks:
            with self.subTest(mutation=mutate):
                github = GitHub()
                mutate(github)
                with (
                    patch.object(source.requests, "get", github.get),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    release = source.discover_latest()
                    with self.assertRaises(source.ReleaseIntegrityError):
                        source.stage_release(release, Path(tmp) / "source")
                    self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_real_archive_rejects_link_traversal_unexpected_files_and_large_content(
        self,
    ):
        attacks = [
            {"src/escape": (tarfile.SYMTYPE, "../../config/config.yaml")},
            {"src/hardlink": (tarfile.LNKTYPE, "README.md")},
            {"/config/config.yaml": b"override"},
            {"src/../../config/config.yaml": b"override"},
            {"config/config.yaml": b"secret"},
            {"src/.venv/bin/python": b"bad"},
            {"src/desk_controller/_source_version.json": b"bad"},
        ]
        for extra in attacks:
            with self.subTest(extra=extra):
                github = GitHub(archive=archive_with(extra))
                with (
                    patch.object(source.requests, "get", github.get),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    release = source.discover_latest()
                    with self.assertRaises(source.ReleaseIntegrityError):
                        source.stage_release(release, Path(tmp) / "source")
                    self.assertEqual(list(Path(tmp).iterdir()), [])
        github = GitHub(archive=archive_with({"src/huge.txt": b"x" * (64 * 1024)}))
        with (
            patch.object(source.requests, "get", github.get),
            patch.object(source, "MAX_EXPANDED", 32 * 1024),
            tempfile.TemporaryDirectory() as tmp,
        ):
            release = source.discover_latest()
            with self.assertRaises(source.ReleaseIntegrityError):
                source.stage_release(release, Path(tmp) / "source")
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
