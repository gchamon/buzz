import unittest
import tempfile
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from buzz.core.subtitles import (
    release_similarity,
    _apply_filters,
    _source_matches_torrent,
    rank_subtitles,
    get_search_params,
    apply_subtitle_overlay,
    fetch_subtitles_for_library
)
from buzz.models import PresentationConfig, SubtitleConfig, SubtitleFilters

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
            {"attributes": {"download_count": 100, "new_download_count": 50, "release": "A"}},
            {"attributes": {"download_count": 200, "new_download_count": 10, "release": "B"}},
        ]
        f = SubtitleFilters()
        best = rank_subtitles(results, "most-downloaded", f, "source")
        self.assertIsNotNone(best)
        if best:
            self.assertEqual(best["attributes"]["release"], "B")

    def test_rank_subtitles_best_rated(self):
        results = [
            {"attributes": {"ratings": 4.5, "votes": 10, "release": "A"}},
            {"attributes": {"ratings": 5.0, "votes": 1, "release": "B"}},
            {"attributes": {"ratings": 5.0, "votes": 0, "release": "C"}},
        ]
        f = SubtitleFilters()
        best = rank_subtitles(results, "best-rated", f, "source")
        self.assertIsNotNone(best)
        if best:
            self.assertEqual(best["attributes"]["release"], "B") # C is ignored because votes=0

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

    @patch("buzz.core.subtitles.OpenSubtitlesClient")
    def test_fetch_subtitles_e2e_logic(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.search.return_value = [
            {"attributes": {"release": "Movie.2024.srt", "download_count": 100, "files": [{"file_id": 123}]}}
        ]
        mock_client.download.return_value = "http://download"
        mock_client.fetch_content.return_value = b"subtitle content"
        
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = PresentationConfig(
                source_root=root / "raw",
                target_root=root / "curated",
                state_root=root / "state",
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

    def test_source_matches_torrent(self):
        self.assertTrue(_source_matches_torrent("movies/MyMovie/MyMovie.mkv", "MyMovie"))
        self.assertTrue(_source_matches_torrent("shows/MySeries/Season 01/ep.mkv", "MySeries"))
        self.assertFalse(_source_matches_torrent("movies/OtherMovie/file.mkv", "MyMovie"))
        # Single component path should not match
        self.assertFalse(_source_matches_torrent("movies", "movies"))

if __name__ == "__main__":
    unittest.main()
