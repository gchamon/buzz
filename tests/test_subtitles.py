import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buzz.core import db
from buzz.core.subtitles import (
    _apply_filters,
    _read_subtitle_meta,
    _source_matches_torrent,
    _write_subtitle_meta,
    apply_subtitle_overlay,
    fetch_subtitles_for_library,
    get_search_params,
    rank_subtitles,
    release_similarity,
)
from buzz.models import CuratorConfig, SubtitleConfig, SubtitleFilters


class SubtitleTests(unittest.TestCase):
    def test_release_similarity(self):
        self.assertAlmostEqual(release_similarity("Movie.2024.1080p.mkv", "Movie.2024.1080p.BluRay.srt"), 0.5, places=1)
        self.assertEqual(release_similarity("a b c", "a b c"), 1.0)
        self.assertEqual(release_similarity("a b c", "d e f"), 0.0)

    def test_apply_filters(self):
        results = [
            {"attributes": {"hearing_impaired": True, "release": "HI"}},
            {"attributes": {"hearing_impaired": False, "release": "Regular"}},
            {"attributes": {"ai_translated": True, "release": "AI"}},
            {"attributes": {"machine_translated": True, "release": "Machine"}},
        ]

        f = SubtitleFilters(hearing_impaired="exclude", exclude_ai=True, exclude_machine=True)
        filtered = _apply_filters(results, f)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["attributes"]["release"], "Regular")

        f = SubtitleFilters(hearing_impaired="include", exclude_ai=False, exclude_machine=False)
        filtered = _apply_filters(results, f)
        self.assertEqual(len(filtered), 4)

    def test_rank_subtitles_most_downloaded(self):
        results = [
            {"attributes": {
                "download_count": 100,
                "new_download_count": 50,
                "release": "Source.Release.A",
                "feature_details": {"title": "Source Movie"}
            }},
            {"attributes": {
                "download_count": 200,
                "new_download_count": 10,
                "release": "Source.Release.B",
                "feature_details": {"title": "Source Movie"}
            }},
        ]
        f = SubtitleFilters()
        best = rank_subtitles(results, "most-downloaded", f, "Source.Release.mkv", query="Source Movie")
        self.assertIsNotNone(best)
        if best:
            self.assertEqual(best["attributes"]["release"], "Source.Release.B")

    def test_rank_subtitles_best_rated(self):
        results = [
            {"attributes": {"ratings": 4.5, "votes": 10, "release": "A", "feature_details": {"title": "Source Movie"}}},
            {"attributes": {"ratings": 5.0, "votes": 1, "release": "B", "feature_details": {"title": "Source Movie"}}},
            {"attributes": {"ratings": 5.0, "votes": 0, "release": "C", "feature_details": {"title": "Source Movie"}}},
        ]
        f = SubtitleFilters()
        best = rank_subtitles(results, "best-rated", f, "source", query="Source Movie")
        self.assertIsNotNone(best)
        if best:
            self.assertEqual(best["attributes"]["release"], "B") # C is ignored because votes=0

    def test_result_matches_query(self):
        from buzz.core.subtitles import _result_matches_query

        # Exact match
        res = {"attributes": {"feature_details": {"title": "The Matrix", "year": 1999}}}
        self.assertTrue(_result_matches_query(res, "The Matrix", 1999))

        # Case insensitive / tokens
        self.assertTrue(_result_matches_query(res, "the.matrix", 1999))

        # Year mismatch (more than 1 year)
        self.assertFalse(_result_matches_query(res, "The Matrix", 2005))

        # Year match (+/- 1)
        self.assertTrue(_result_matches_query(res, "The Matrix", 1998))
        self.assertTrue(_result_matches_query(res, "The Matrix", 2000))

        # Wrong title
        res_wrong = {"attributes": {"feature_details": {"title": "Inception", "year": 2010}}}
        self.assertFalse(_result_matches_query(res_wrong, "The Matrix", 1999))

    def test_rank_subtitles_low_relevance_skip(self):
        # Result for wrong movie that somehow passed _result_matches_query (e.g. title missing)
        # but release name is completely different
        results = [
            {"attributes": {
                "download_count": 1000,
                "release": "Completely.Different.Movie.Release-GRP",
                "feature_details": {"title": "Different Movie"}
            }}
        ]
        f = SubtitleFilters()
        # Should return None because similarity is very low and title_sim is low
        best = rank_subtitles(results, "most-downloaded", f, "My.Movie.2024.1080p.mkv", query="My Movie")
        self.assertIsNone(best)

    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_search_sends_type_parameter(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = []

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs"
            )

            # Test Movie
            mapping_movie = [{"type": "movie", "source": "movies/M.mkv", "target": "movies/M/M.mkv"}]
            fetch_subtitles_for_library(config, mapping_movie)
            args, kwargs = mock_client.search.call_args
            self.assertEqual(kwargs["type"], "movie")

            # Test Show
            mapping_show = [{"type": "show", "source": "shows/S/E1.mkv", "target": "shows/S/S01/S01E01.mkv"}]
            fetch_subtitles_for_library(config, mapping_show)
            args, kwargs = mock_client.search.call_args
            self.assertEqual(kwargs["type"], "episode")

    def test_get_search_params(self):
        # Movie
        entry = {"type": "movie", "target": "movies/Movie Name (2024)/Movie Name (2024).mkv"}
        params = get_search_params(entry)
        self.assertEqual(params, {"query": "Movie Name", "year": 2024})

        # Show
        entry = {"type": "show", "target": "shows/Series Name/Season 01/Series Name S01E05.mkv"}
        params = get_search_params(entry)
        self.assertEqual(params, {"query": "Series Name", "season": 1, "episode": 5})

    def test_apply_subtitle_overlay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subs = root / "subs"
            tmp_build = root / "tmp_build"

            sub_file = subs / "movies/Movie (2024)/Movie.en.srt"
            sub_file.parent.mkdir(parents=True)
            sub_file.write_text("content")

            apply_subtitle_overlay(tmp_build, subs)

            target = tmp_build / "movies/Movie (2024)/Movie.en.srt"
            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), str(sub_file))

    @patch("buzz.core.subtitles.trigger_jellyfin_selective_refresh")
    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_fetch_subtitles_e2e_logic(self, mock_client_cls, mock_refresh):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = [
            {"attributes": {"release": "Movie.2024.srt", "download_count": 100, "files": [{"file_id": 123}]}}
        ]
        mock_client.download.return_value = "http://download"
        mock_client.fetch_content.return_value = b"subtitle content"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs"
            )

            mapping = [
                {"type": "movie", "source": "movies/Movie.2024.mkv", "target": "movies/Movie (2024)/Movie (2024).mkv"}
            ]

            fetch_subtitles_for_library(config, mapping)

            sub_file = root / "subs/movies/Movie (2024)/Movie (2024).en.srt"
            self.assertTrue(sub_file.exists())
            self.assertEqual(sub_file.read_text(), "subtitle content")

            # Check curated symlink
            curated_sub = root / "curated/movies/Movie (2024)/Movie (2024).en.srt"
            self.assertTrue(curated_sub.is_symlink())

    @patch("buzz.core.subtitles.trigger_jellyfin_selective_refresh")
    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_fetch_subtitles_triggers_jellyfin_scan(self, mock_client_cls, mock_refresh):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = [
            {"attributes": {"release": "Movie.2024.srt", "download_count": 100, "files": [{"file_id": 123}]}}
        ]
        mock_client.download.return_value = "http://download"
        mock_client.fetch_content.return_value = b"subtitle content"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs",
                jellyfin_api_key="jf_key",
                trigger_lib_scan=True,
            )

            mapping = [
                {"type": "movie", "source": "movies/Movie.2024.mkv", "target": "movies/Movie (2024)/Movie (2024).mkv"}
            ]

            fetch_subtitles_for_library(config, mapping)

            # Should be called with config and the list of target paths that got new subtitles
            mock_refresh.assert_called_once_with(config, ["movies/Movie (2024)/Movie (2024).mkv"])

    def test_source_matches_torrent(self):
        self.assertTrue(_source_matches_torrent("movies/MyMovie/MyMovie.mkv", "MyMovie"))
        self.assertTrue(_source_matches_torrent("shows/MySeries/Season 01/ep.mkv", "MySeries"))
        self.assertFalse(_source_matches_torrent("movies/OtherMovie/file.mkv", "MyMovie"))
        # Single component path should not match
        self.assertFalse(_source_matches_torrent("movies", "movies"))

    def test_subtitle_meta_helpers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs",
            )
            sub_path = config.subtitle_root / "movie.en.srt"

            # Read non-existent meta returns None
            self.assertIsNone(_read_subtitle_meta(config, sub_path))

            # Write and read back
            _write_subtitle_meta(
                config,
                sub_path,
                {"file_id": 123, "release": "Test.Release"},
            )
            conn = db.connect(config.state_dir / "buzz.sqlite")
            db.apply_migrations(conn)
            try:
                rows = conn.execute(
                    "SELECT overlay_path FROM subtitle_metadata"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual([row["overlay_path"] for row in rows], ["movie.en.srt"])
            meta = _read_subtitle_meta(config, sub_path)
            self.assertEqual(meta, {"file_id": 123, "release": "Test.Release"})

    @patch("buzz.core.subtitles.trigger_jellyfin_selective_refresh")
    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_fetch_subtitles_skips_when_meta_matches(self, mock_client_cls, mock_refresh):
        """If subtitle exists and metadata file_id matches, skip downloading."""
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = [
            {"attributes": {"release": "Movie.2024.srt", "download_count": 100, "files": [{"file_id": 123}]}}
        ]
        mock_client.download.return_value = "http://download"
        mock_client.fetch_content.return_value = b"new subtitle content"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs"
            )

            # Pre-create existing subtitle with matching metadata
            sub_file = root / "subs/movies/Movie (2024)/Movie (2024).en.srt"
            sub_file.parent.mkdir(parents=True)
            sub_file.write_text("existing content")
            _write_subtitle_meta(
                config,
                sub_file,
                {"file_id": 123, "release": "Movie.2024.srt"},
            )

            mapping = [
                {"type": "movie", "source": "movies/Movie.2024.mkv", "target": "movies/Movie (2024)/Movie (2024).mkv"}
            ]

            fetch_subtitles_for_library(config, mapping)

            # Should have searched but NOT downloaded
            mock_client.search.assert_called_once()
            mock_client.download.assert_not_called()
            self.assertEqual(sub_file.read_text(), "existing content")

    @patch("buzz.core.subtitles.trigger_jellyfin_selective_refresh")
    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_fetch_subtitles_replaces_when_meta_mismatches(self, mock_client_cls, mock_refresh):
        """If subtitle exists but metadata file_id differs, replace it."""
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = [
            {"attributes": {"release": "Movie.2024.srt", "download_count": 100, "files": [{"file_id": 999}]}}
        ]
        mock_client.download.return_value = "http://download"
        mock_client.fetch_content.return_value = b"new subtitle content"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs"
            )

            # Pre-create existing subtitle with OLD metadata
            sub_file = root / "subs/movies/Movie (2024)/Movie (2024).en.srt"
            sub_file.parent.mkdir(parents=True)
            sub_file.write_text("old content")
            _write_subtitle_meta(
                config,
                sub_file,
                {"file_id": 123, "release": "Old.Release"},
            )

            mapping = [
                {"type": "movie", "source": "movies/Movie.2024.mkv", "target": "movies/Movie (2024)/Movie (2024).mkv"}
            ]

            fetch_subtitles_for_library(config, mapping)

            # Should have searched AND downloaded
            mock_client.search.assert_called_once()
            mock_client.download.assert_called_once()
            self.assertEqual(sub_file.read_text(), "new subtitle content")

            # Metadata should be updated
            meta = _read_subtitle_meta(config, sub_file)
            if meta is None:
                self.fail("Expected subtitle metadata after replacement")
            self.assertEqual(meta["file_id"], 999)
            self.assertEqual(meta["release"], "Movie.2024.srt")

            # Curated symlink should exist and point to new file
            curated_sub = root / "curated/movies/Movie (2024)/Movie (2024).en.srt"
            self.assertTrue(curated_sub.is_symlink())

    @patch("buzz.core.subtitles.trigger_jellyfin_selective_refresh")
    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_fetch_subtitles_replaces_when_no_meta(self, mock_client_cls, mock_refresh):
        """If subtitle exists but has no metadata, replace it."""
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = [
            {"attributes": {"release": "Movie.2024.srt", "download_count": 100, "files": [{"file_id": 999}]}}
        ]
        mock_client.download.return_value = "http://download"
        mock_client.fetch_content.return_value = b"new subtitle content"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = CuratorConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_dir=root / "state",
                subtitles=SubtitleConfig(enabled=True, api_key="key"),
                subtitle_root=root / "subs"
            )

            # Pre-create existing subtitle WITHOUT metadata
            sub_file = root / "subs/movies/Movie (2024)/Movie (2024).en.srt"
            sub_file.parent.mkdir(parents=True)
            sub_file.write_text("old content")

            mapping = [
                {"type": "movie", "source": "movies/Movie.2024.mkv", "target": "movies/Movie (2024)/Movie (2024).mkv"}
            ]

            fetch_subtitles_for_library(config, mapping)

            # Should have searched AND downloaded
            mock_client.search.assert_called_once()
            mock_client.download.assert_called_once()
            self.assertEqual(sub_file.read_text(), "new subtitle content")

            # Metadata should be written
            meta = _read_subtitle_meta(config, sub_file)
            if meta is None:
                self.fail("Expected subtitle metadata after download")
            self.assertEqual(meta["file_id"], 999)

if __name__ == "__main__":
    unittest.main()
