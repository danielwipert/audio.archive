from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from audio_archive.archive_verify import verify_archive_item
from audio_archive.integrity import write_sha256sums
from audio_archive.manifest import write_manifest_atomic
from audio_archive.verify import sha256_file


def make_item(root: Path) -> tuple[Path, Path]:
    item = root / "item"
    master = item / "master" / "source.webm"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"source")
    manifest = {
        "schema_version": "1.2",
        "archive_id": "youtube:dQw4w9WgXcQ",
        "content_type": "song",
        "request": {},
        "resolution": {},
        "source": {},
        "acquisition": {},
        "source_master": {
            "role": "source_master",
            "path": "master/source.webm",
            "sha256": sha256_file(master),
        },
        "intermediates": [],
        "derivatives": [],
    }
    manifest_path = item / "metadata" / "archive.json"
    write_manifest_atomic(manifest_path, manifest)
    write_sha256sums(item, [Path("master/source.webm"), Path("metadata/archive.json")])
    return item, manifest_path


class ArchiveVerificationTests(unittest.TestCase):
    def test_valid_item_cross_checks_manifest_and_checksum_inventory(self) -> None:
        with TemporaryDirectory() as directory:
            item, _ = make_item(Path(directory))
            result = verify_archive_item(item)
        self.assertTrue(result.valid)
        self.assertEqual(result.checked_files, 2)

    def test_manifest_asset_missing_from_checksum_inventory_fails(self) -> None:
        with TemporaryDirectory() as directory:
            item, manifest_path = make_item(Path(directory))
            derivative = item / "intermediates" / "ableton" / "source.wav"
            derivative.parent.mkdir(parents=True)
            derivative.write_bytes(b"wav")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["intermediates"].append(
                {
                    "role": "ableton",
                    "path": "intermediates/ableton/source.wav",
                    "sha256": sha256_file(derivative),
                }
            )
            write_manifest_atomic(manifest_path, manifest)
            write_sha256sums(item, [Path("master/source.webm"), Path("metadata/archive.json")])
            result = verify_archive_item(item)
        self.assertFalse(result.valid)
        self.assertIn(
            "manifest asset is absent from SHA256SUMS: intermediates/ableton/source.wav",
            result.errors,
        )

    def test_tampered_file_fails_both_recorded_integrity_checks(self) -> None:
        with TemporaryDirectory() as directory:
            item, _ = make_item(Path(directory))
            (item / "master" / "source.webm").write_bytes(b"tampered")
            result = verify_archive_item(item)
        self.assertFalse(result.valid)
        self.assertIn("checksum mismatch: master/source.webm", result.errors)
        self.assertIn("manifest checksum mismatch: master/source.webm", result.errors)


if __name__ == "__main__":
    unittest.main()
