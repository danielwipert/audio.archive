import unittest

from audio_archive.manifest import validate_manifest


class ManifestTests(unittest.TestCase):
    def test_required_v12_shape_is_accepted(self) -> None:
        result = validate_manifest(
            {
                "schema_version": "1.2",
                "archive_id": "youtube:AAAAAAAAAAA",
                "content_type": "song",
                "request": {},
                "resolution": {},
                "source": {},
                "acquisition": {},
                "source_master": {},
                "intermediates": [],
                "derivatives": [],
            }
        )
        self.assertTrue(result.valid)

    def test_missing_fields_are_rejected(self) -> None:
        result = validate_manifest({"schema_version": "1.2", "archive_id": "youtube:x"})
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()

