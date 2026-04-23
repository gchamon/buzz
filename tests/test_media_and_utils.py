"""Tests for media parsing and utility functions."""

import unittest

from buzz.core.media import (
    is_probably_media_content_type,
    looks_like_markup,
    parse_movie,
    parse_show,
)
from buzz.core.utils import format_bytes


class FormatBytesTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")

    def test_kib(self):
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_bytes(1536), "1.5 KiB")

    def test_mib(self):
        self.assertEqual(format_bytes(1024 ** 2), "1.0 MiB")

    def test_gib(self):
        self.assertEqual(format_bytes(1024 ** 3), "1.0 GiB")

    def test_tib(self):
        self.assertEqual(format_bytes(1024 ** 4), "1.0 TiB")

    def test_non_numeric_returns_zero(self):
        """Non-numeric input should gracefully return '0 B'."""
        self.assertEqual(format_bytes(None), "0 B")
        self.assertEqual(format_bytes("abc"), "0 B")
        self.assertEqual(format_bytes([]), "0 B")


class ParseMovieTests(unittest.TestCase):
    def test_simple_movie(self):
        result = parse_movie("The.Matrix.1999.1080p.mkv")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "The Matrix")
        self.assertEqual(result["year"], 1999)

    def test_show_pattern_rejected(self):
        """Files with SxxExx should not parse as movies."""
        self.assertIsNone(parse_movie("Show.S01E01.mkv"))

    def test_year_only_in_folder(self):
        result = parse_movie(
            "The.Movie.mkv", folder="The Movie 2020 BluRay"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["year"], 2020)

    def test_no_year_returns_none(self):
        self.assertIsNone(parse_movie("Some.Random.File.mkv"))

    def test_stem_with_year_at_start(self):
        """Cases like '2001 - A Space Odyssey' should work."""
        result = parse_movie("2001.A.Space.Odyssey.1968.mkv")
        self.assertIsNotNone(result)
        self.assertEqual(result["year"], 1968)


class ParseShowTests(unittest.TestCase):
    def test_standard_pattern(self):
        result = parse_show("Show.Name.S03E12.1080p.mkv")
        self.assertIsNotNone(result)
        self.assertEqual(result["series"], "Show Name")
        self.assertEqual(result["season"], 3)
        self.assertEqual(result["episode"], 12)

    def test_alternate_pattern(self):
        result = parse_show("Show.Name.2x05.1080p.mkv")
        self.assertIsNotNone(result)
        self.assertEqual(result["series"], "Show Name")
        self.assertEqual(result["season"], 2)
        self.assertEqual(result["episode"], 5)

    def test_no_match_returns_none(self):
        self.assertIsNone(parse_show("Some.Movie.2020.mkv"))


class ContentTypeTests(unittest.TestCase):
    def test_empty_is_media(self):
        self.assertTrue(is_probably_media_content_type(None))
        self.assertTrue(is_probably_media_content_type(""))

    def test_video_prefixes(self):
        self.assertTrue(is_probably_media_content_type("video/mp4"))
        self.assertTrue(is_probably_media_content_type("audio/mp3"))

    def test_non_media_rejected(self):
        self.assertFalse(
            is_probably_media_content_type("text/html")
        )


class MarkupDetectionTests(unittest.TestCase):
    def test_html_detected(self):
        self.assertTrue(looks_like_markup(b"<!DOCTYPE html>"))
        self.assertTrue(looks_like_markup(b"<html>"))

    def test_xml_detected(self):
        self.assertTrue(looks_like_markup(b"<?xml version=\"1.0\"?>"))

    def test_json_detected(self):
        self.assertTrue(looks_like_markup(b"{\"key\": \"value\"}"))

    def test_media_bytes_not_markup(self):
        self.assertFalse(looks_like_markup(b"\x00\x00\x00\x00"))
        self.assertFalse(looks_like_markup(b"some plain text"))


if __name__ == "__main__":
    unittest.main()
