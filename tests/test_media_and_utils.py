"""Tests for media parsing and utility functions."""

import unittest

from buzz.core.media import (
    is_probably_media_content_type,
    looks_like_markup,
    parse_movie,
    parse_show,
)
from buzz.core.utils import format_bytes, magnet_display_name


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
        if result is None:
            self.fail("Expected movie parse result")
        self.assertEqual(result["title"], "The Matrix")
        self.assertEqual(result["year"], 1999)

    def test_show_pattern_rejected(self):
        """Files with SxxExx should not parse as movies."""
        self.assertIsNone(parse_movie("Show.S01E01.mkv"))

    def test_year_only_in_folder(self):
        result = parse_movie(
            "The.Movie.mkv", folder="The Movie 2020 BluRay"
        )
        if result is None:
            self.fail("Expected movie parse result from folder")
        self.assertEqual(result["year"], 2020)

    def test_no_year_returns_none(self):
        self.assertIsNone(parse_movie("Some.Random.File.mkv"))

    def test_stem_with_year_at_start(self):
        """Cases like '2001 - A Space Odyssey' should work."""
        result = parse_movie("2001.A.Space.Odyssey.1968.mkv")
        if result is None:
            self.fail("Expected movie parse result for year-at-start title")
        self.assertEqual(result["year"], 1968)


class ParseShowTests(unittest.TestCase):
    def test_standard_pattern(self):
        result = parse_show("Show.Name.S03E12.1080p.mkv")
        if result is None:
            self.fail("Expected show parse result")
        self.assertEqual(result["series"], "Show Name")
        self.assertEqual(result["season"], 3)
        self.assertEqual(result["episode"], 12)

    def test_standard_pattern_with_year(self):
        result = parse_show("Adventure Time (2008) - S00E01 - Pilot.mkv")
        if result is None:
            self.fail("Expected show parse result")
        self.assertEqual(result["series"], "Adventure Time")
        self.assertEqual(result["year"], 2008)
        self.assertEqual(result["season"], 0)
        self.assertEqual(result["episode"], 1)

    def test_alternate_pattern(self):
        result = parse_show("Show.Name.2x05.1080p.mkv")
        if result is None:
            self.fail("Expected alternate show parse result")
        self.assertEqual(result["series"], "Show Name")
        self.assertEqual(result["season"], 2)
        self.assertEqual(result["episode"], 5)

    def test_season_episode_words_pattern(self):
        result = parse_show(
            "Cobalt City (2005) Season 1 Episode 01 2160p H.264.mkv"
        )
        if result is None:
            self.fail("Expected season/episode words show parse result")
        self.assertEqual(result["series"], "Cobalt City")
        self.assertEqual(result["year"], 2005)
        self.assertEqual(result["season"], 1)
        self.assertEqual(result["episode"], 1)

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


class MagnetDisplayNameTests(unittest.TestCase):
    def test_full_magnet_returns_dn(self):
        magnet = "magnet:?xt=urn:btih:abc123&dn=The.Movie.2024.1080p&tr=udp://tracker"
        self.assertEqual(magnet_display_name(magnet), "The.Movie.2024.1080p")

    def test_url_encoded_dn_decoded(self):
        magnet = "magnet:?xt=urn:btih:abc123&dn=Some%20Show%20S01"
        self.assertEqual(magnet_display_name(magnet), "Some Show S01")

    def test_plus_encoded_spaces_decoded(self):
        magnet = "magnet:?xt=urn:btih:abc123&dn=Some+Show+S01"
        self.assertEqual(magnet_display_name(magnet), "Some Show S01")

    def test_hash_only_magnet_returns_empty(self):
        magnet = "magnet:?xt=urn:btih:a7b063a88ef3f87704f071f24a615062b97ff60a"
        self.assertEqual(magnet_display_name(magnet), "")

    def test_empty_string_returns_empty(self):
        self.assertEqual(magnet_display_name(""), "")

    def test_non_magnet_returns_empty(self):
        self.assertEqual(magnet_display_name("https://example.com"), "")

    def test_slashes_sanitized(self):
        magnet = "magnet:?xt=urn:btih:abc123&dn=Some/Path/Name"
        self.assertEqual(magnet_display_name(magnet), "Some Path Name")


if __name__ == "__main__":
    unittest.main()
