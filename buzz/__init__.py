"""Buzz package."""

import sys

if "unittest" in sys.modules:
    import unittest

    _original_runner_init = unittest.TextTestRunner.__init__

    def _buffered_runner_init(self, *args, **kwargs):
        kwargs["buffer"] = True
        return _original_runner_init(self, *args, **kwargs)

    unittest.TextTestRunner.__init__ = _buffered_runner_init
