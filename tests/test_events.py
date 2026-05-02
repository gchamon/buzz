import unittest
from contextlib import redirect_stdout
from io import StringIO
from typing import Any, cast

from buzz.core.events import EventRegistry
from buzz.dav_app import DavApp


class EventRegistryTests(unittest.TestCase):
    def test_record_and_get_recent(self):
        registry = EventRegistry(maxlen=5)
        registry.record("msg 1")
        registry.record("msg 2", level="warning")

        events = registry.get_recent()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["message"], "msg 1")
        self.assertEqual(events[0]["level"], "info")
        self.assertEqual(events[1]["message"], "msg 2")
        self.assertEqual(events[1]["level"], "warning")
        self.assertIn("timestamp", events[0])

    def test_ring_buffer_behavior(self):
        registry = EventRegistry(maxlen=3)
        for i in range(5):
            registry.record(f"msg {i}")

        events = registry.get_recent()
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["message"], "msg 2")
        self.assertEqual(events[-1]["message"], "msg 4")

    def test_get_recent_limit(self):
        registry = EventRegistry(maxlen=10)
        for i in range(10):
            registry.record(f"msg {i}")

        events = registry.get_recent(limit=3)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["message"], "msg 7")
        self.assertEqual(events[-1]["message"], "msg 9")

    def test_consecutive_identical_warnings_are_counted(self):
        registry = EventRegistry(maxlen=10)

        with redirect_stdout(StringIO()) as stdout:
            registry.record("rd unavailable", level="warning", source="dav")
            registry.record("rd unavailable", level="warning", source="dav")
            registry.record("rd unavailable", level="warning", source="dav")

        events = registry.get_recent()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "rd unavailable")
        self.assertEqual(events[0]["count"], 3)
        self.assertEqual(stdout.getvalue().count("rd unavailable"), 1)

    def test_warning_sequence_breaks_on_different_identity(self):
        registry = EventRegistry(maxlen=10)
        registry.record("rd unavailable", level="warning", source="dav")
        registry.record("sync failed", level="warning", source="dav")
        registry.record("rd unavailable", level="warning", source="dav")
        registry.record("rd unavailable", level="warning", source="curator")

        events = registry.get_recent()
        self.assertEqual(len(events), 4)
        self.assertEqual([event["count"] for event in events], [1, 1, 1, 1])

    def test_non_warning_events_keep_separate_entries(self):
        registry = EventRegistry(maxlen=10)
        registry.record("boom", level="error")
        registry.record("boom", level="error")

        events = registry.get_recent()
        self.assertEqual(len(events), 2)
        self.assertEqual([event["count"] for event in events], [1, 1])

    def test_formatted_logs_append_warning_count(self):
        class FakeApp:
            def get_logs(self, limit: int = 100):
                return [
                    {
                        "timestamp": "2026-04-30T10:40:09Z",
                        "level": "warning",
                        "message": "background sync failed: eof",
                        "source": "dav",
                        "count": 3,
                    }
                ]

        formatted = DavApp.formatted_logs(cast(Any, FakeApp()), limit=100)

        self.assertEqual(
            formatted[0]["message"],
            "background sync failed: eof (3)",
        )
        self.assertIn(
            "[WARNING] background sync failed: eof (3)",
            formatted[0]["copy_text"],
        )

    def test_clear_removes_existing_events(self):
        registry = EventRegistry(maxlen=10)
        registry.record("first")
        registry.record("second")

        registry.clear()
        registry.record("third")

        events = registry.get_recent()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "third")

    def test_formatted_logs_are_newest_first(self):
        class FakeApp:
            def get_logs(self, limit: int = 100):
                return [
                    {
                        "timestamp": "2026-04-30T10:40:09Z",
                        "level": "info",
                        "message": "older",
                        "source": "dav",
                    },
                    {
                        "timestamp": "2026-04-30T10:41:09Z",
                        "level": "info",
                        "message": "newer",
                        "source": "dav",
                    },
                ]

        formatted = DavApp.formatted_logs(cast(Any, FakeApp()), limit=100)

        self.assertEqual(
            [item["message"] for item in formatted],
            ["newer", "older"],
        )


if __name__ == "__main__":
    unittest.main()
