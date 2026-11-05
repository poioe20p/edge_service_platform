"""pymongo CommandListener that accumulates per-request read/write DB time."""
from pymongo import monitoring
from flask import g

_READ_CMDS = {
    "find", "aggregate", "count", "distinct",
    "getMore", "findAndModify",
}
_WRITE_CMDS = {"insert", "update", "delete"}
_IGNORED_CMDS = {
    "hello", "isMaster", "ismaster", "ping", "buildInfo",
    "saslStart", "saslContinue", "endSessions",
    "getParameter", "killCursors", "listDatabases",
    "listCollections", "listIndexes", "connectionStatus",
}


def _command_target(command_name: str, command: dict) -> str | None:
    target = command.get(command_name)
    if isinstance(target, str):
        return target
    collection = command.get("collection")
    if isinstance(collection, str):
        return collection
    return None


class _DbTimingListener(monitoring.CommandListener):
    def started(self, event):
        if event.command_name in _IGNORED_CMDS:
            return
        try:
            g.db_last_command = event.command_name
            g.db_last_command_db = getattr(event, "database_name", None)
            g.db_last_command_target = _command_target(
                event.command_name,
                getattr(event, "command", {}) or {},
            )
            g.db_last_command_failed = None
            g.db_last_command_duration_s = None
        except RuntimeError:
            # Outside Flask request context (driver-internal op) — ignore.
            pass

    def succeeded(self, event):
        self._record(event.command_name, event.duration_micros, failed=False)

    def failed(self, event):
        self._record(event.command_name, event.duration_micros, failed=True)

    @staticmethod
    def _record(cmd: str, dur_us: int, *, failed: bool) -> None:
        if cmd in _IGNORED_CMDS:
            return
        dur_s = dur_us / 1_000_000.0
        try:
            g.db_last_command = cmd
            g.db_last_command_failed = failed
            g.db_last_command_duration_s = dur_s
            if cmd in _READ_CMDS:
                g.time_db_read_s = getattr(g, "time_db_read_s", 0.0) + dur_s
            elif cmd in _WRITE_CMDS:
                g.time_db_write_s = getattr(g, "time_db_write_s", 0.0) + dur_s
            g.time_db_cmd_count = getattr(g, "time_db_cmd_count", 0) + 1
        except RuntimeError:
            # Outside Flask request context (driver-internal op) — ignore.
            pass


_listener_registered = False


def register() -> None:
    """Idempotent global registration. Call once at app import time."""
    global _listener_registered
    if _listener_registered:
        return
    monitoring.register(_DbTimingListener())
    _listener_registered = True
