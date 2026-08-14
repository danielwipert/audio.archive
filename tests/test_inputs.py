from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audio_archive.inputs import normalize_request, preview_csv
from audio_archive.urls import parse_youtube_url


class UrlTests(unittest.TestCase):
    def test_supported_urls_are_canonicalized(self) -> None:
        urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=30",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/shorts/dQw4w9WgXcQ",
        ]
        for url in urls:
            with self.subTest(url=url):
                parsed = parse_youtube_url(url)
                self.assertEqual(parsed.video_id, "dQw4w9WgXcQ")
                self.assertEqual(
                    parsed.canonical_url,
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                )

    def test_non_youtube_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_youtube_url("https://example.com/watch?v=dQw4w9WgXcQ")


class CsvTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "batch.csv"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_rows_survive_invalid_rows_and_duplicates_collapse(self) -> None:
        path = self._write(
            "artist,title,version,url,profile\n"
            "Massive Attack,Teardrop,,,\n"
            ",,,,ableton\n"
            "Massive Attack,Teardrop,,,\n"
            ",,,https://youtu.be/dQw4w9WgXcQ,archive\n"
        )
        preview = preview_csv(path)
        self.assertEqual(len(preview.accepted), 2)
        self.assertEqual(preview.rejected[0].row_number, 3)
        self.assertEqual(preview.duplicate_rows, (4,))
        self.assertEqual(preview.accepted[1].profile, "archive")

    def test_headers_are_trimmed_and_case_insensitive(self) -> None:
        path = self._write(" Artist , TITLE \nPortishead,Roads\n")
        preview = preview_csv(path)
        self.assertEqual(preview.accepted[0].artist, "Portishead")

    def test_request_requires_url_or_artist_and_title(self) -> None:
        with self.assertRaises(ValueError):
            normalize_request(artist="Radiohead")


if __name__ == "__main__":
    unittest.main()
