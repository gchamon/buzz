import unittest

from buzz.core.events import EventRegistry


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

if __name__ == "__main__":
    unittest.main()
