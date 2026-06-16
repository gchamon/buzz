"""Console bar write API for the operator UI.

The console bar rendered by ``partials/meta_bar.html`` is driven by the
``console_msg`` and ``console_class`` template variables.  Use this module
to write to it — never reference ``socket.context`` keys or CSS class strings
directly.

The console bar is a per-page, ephemeral status line.  For structured,
persistent event records visible in the Logs UI, use ``buzz.events``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import TaskStatus


class Level:
    SUCCESS = "service-status-green"
    ERROR   = "service-status-red"
    WARNING = "service-status-orange"
    PENDING = "service-status-orange"
    RESTART = "service-status-yellow"
    INFO    = ""

    @staticmethod
    def task_status_class(status: TaskStatus | str) -> str:
        if status in {"running", "queued", "cancelling"}:
            return Level.RESTART
        if status == "pending":
            return Level.RESTART
        if status == "failed":
            return Level.ERROR
        if status == "aborted":
            return Level.ERROR
        if status == "complete":
            return Level.SUCCESS
        if status == "cancelled":
            return Level.WARNING
        return "thread-status-muted"


def log(context: Any, message: str, level: str = Level.INFO) -> None:
    """Write *message* and *level* into a live-view context dict."""
    context["console_msg"] = message
    context["console_class"] = level
