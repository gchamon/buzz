import json
import tempfile
import unittest
from pathlib import Path

from buzz.core import db
from buzz.core.state import canonical_snapshot
from buzz.core.utils import stable_json


class DatabaseTests(unittest.TestCase):
    def test_schema_migration_applies_and_is_idempotent(self):
        conn = db.connect(":memory:")
        try:
            db.apply_migrations(conn)
            db.apply_migrations(conn)
            version = conn.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()["version"]
        finally:
            conn.close()
        self.assertEqual(version, 2)

    def test_legacy_json_import_renames_files_to_migrated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            (state_dir / "torrent_cache.json").write_text(
                json.dumps(
                    {
                        "torrent-1": {
                            "signature": {"status": "downloaded"},
                            "info": {"id": "torrent-1", "status": "downloaded"},
                            "magnet": "magnet:?xt=urn:btih:torrent-1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot = {
                "dirs": [""],
                "files": {},
                "report": {"movies": 0, "generated_at": "2026-01-01T00:00:00Z"},
            }
            (state_dir / "library_snapshot.json").write_text(
                json.dumps(snapshot),
                encoding="utf-8",
            )
            (state_dir / "mapping.json").write_text(
                json.dumps(
                    [
                        {
                            "source": "movies/source.mkv",
                            "target": "movies/Target/Target.mkv",
                            "type": "movie",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (state_dir / "report.json").write_text(
                json.dumps({"movies": 1}),
                encoding="utf-8",
            )
            (state_dir / "trashcan.json").write_text(
                json.dumps(
                    {
                        "hash-1": {
                            "name": "Movie",
                            "bytes": 10,
                            "files": [{"id": 1, "path": "movie.mkv"}],
                            "deleted_at": "2026-01-01T00:00:00Z",
                            "magnet": "magnet:?xt=urn:btih:hash-1",
                        }
                    }
                ),
                encoding="utf-8",
            )

            conn = db.connect(state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                db.migrate_legacy_files(conn, state_dir)
                torrent_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM torrents"
                ).fetchone()["count"]
                archive_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM archive"
                ).fetchone()["count"]
                torrent_row = conn.execute(
                    "SELECT magnet FROM torrents WHERE id = 'torrent-1'"
                ).fetchone()
                archive_row = conn.execute(
                    "SELECT magnet FROM archive WHERE hash = 'hash-1'"
                ).fetchone()
                mapping_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM curator_mapping"
                ).fetchone()["count"]
                report = db.load_curator_report(conn)
                snapshot_row = conn.execute(
                    "SELECT digest FROM library_snapshot WHERE singleton = 1"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(torrent_count, 1)
            self.assertEqual(archive_count, 1)
            self.assertEqual(torrent_row["magnet"], "magnet:?xt=urn:btih:torrent-1")
            self.assertEqual(archive_row["magnet"], "magnet:?xt=urn:btih:hash-1")
            self.assertEqual(mapping_count, 1)
            self.assertEqual(report, {"movies": 1})
            self.assertEqual(
                snapshot_row["digest"],
                stable_json(canonical_snapshot(snapshot)),
            )
            for filename in (
                "torrent_cache.migrated",
                "trashcan.migrated",
                "library_snapshot.migrated",
                "mapping.migrated",
                "report.migrated",
            ):
                self.assertTrue((state_dir / filename).exists())

    def test_legacy_import_skipped_when_tables_nonempty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            (state_dir / "mapping.json").write_text(
                json.dumps(
                    [
                        {
                            "source": "movies/old.mkv",
                            "target": "movies/Old/Old.mkv",
                            "type": "movie",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            conn = db.connect(state_dir / "buzz.sqlite")
            try:
                db.apply_migrations(conn)
                db.replace_curator_mapping(
                    conn,
                    [
                        {
                            "source": "movies/new.mkv",
                            "target": "movies/New/New.mkv",
                            "type": "movie",
                        }
                    ],
                )
                db.migrate_legacy_files(conn, state_dir)
                mapping = db.load_curator_mapping(conn)
            finally:
                conn.close()

            self.assertEqual(
                mapping,
                [
                    {
                        "source": "movies/new.mkv",
                        "target": "movies/New/New.mkv",
                        "type": "movie",
                    }
                ],
            )
            self.assertTrue((state_dir / "mapping.json").exists())

    def test_migration_adds_nullable_magnet_columns(self):
        conn = db.connect(":memory:")
        try:
            db.apply_migrations(conn)
            conn.execute(
                "INSERT INTO torrents (id, signature_json, info_json, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("torrent-1", "{}", "{}", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO archive (hash, deleted_at) VALUES (?, ?)",
                ("hash-1", "2026-01-01T00:00:00Z"),
            )
            torrent_row = conn.execute(
                "SELECT magnet FROM torrents WHERE id = 'torrent-1'"
            ).fetchone()
            archive_row = conn.execute(
                "SELECT magnet FROM archive WHERE hash = 'hash-1'"
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNone(torrent_row["magnet"])
        self.assertIsNone(archive_row["magnet"])

    def test_subtitle_metadata_upsert_roundtrip(self):
        conn = db.connect(":memory:")
        try:
            db.apply_migrations(conn)
            db.upsert_subtitle_metadata(
                conn,
                "movies/Movie (2024)/Movie (2024).en.srt",
                {"file_id": 123, "release": "Movie.2024.Release"},
            )
            meta = db.get_subtitle_metadata(
                conn,
                "movies/Movie (2024)/Movie (2024).en.srt",
            )
        finally:
            conn.close()
        self.assertEqual(
            meta,
            {"file_id": 123, "release": "Movie.2024.Release"},
        )

    def test_curator_mapping_replace_replaces_all_rows(self):
        conn = db.connect(":memory:")
        try:
            db.apply_migrations(conn)
            db.replace_curator_mapping(
                conn,
                [
                    {
                        "source": "movies/one.mkv",
                        "target": "movies/One/One.mkv",
                        "type": "movie",
                    }
                ],
            )
            db.replace_curator_mapping(
                conn,
                [
                    {
                        "source": "shows/two.mkv",
                        "target": "shows/Two/Season 01/Two S01E01.mkv",
                        "type": "show",
                    }
                ],
            )
            mapping = db.load_curator_mapping(conn)
        finally:
            conn.close()
        self.assertEqual(
            mapping,
            [
                {
                    "source": "shows/two.mkv",
                    "target": "shows/Two/Season 01/Two S01E01.mkv",
                    "type": "show",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
