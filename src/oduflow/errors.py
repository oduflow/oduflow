class FlowError(Exception):
    """Base error for all Flow operations."""


class BusyError(FlowError):
    """Another operation is already in progress."""


class NotFoundError(FlowError):
    """Environment or resource not found."""


class PrerequisiteNotMetError(FlowError):
    """System not initialized or dependency missing."""


class ProtectedError(FlowError):
    """Environment is protected and cannot be deleted."""


class ConflictError(FlowError):
    """Resource already exists."""


class ConfigError(FlowError):
    """oduflow.toml is missing, unreadable, or fails validation.

    Raised at the settings boundary so a bad config surfaces as a single
    readable line (a CLI error line; MCP: a ToolError) instead of a Python
    traceback in the journal.
    """


class ExternalCommandError(FlowError):
    """External command (git, psql, docker) failed."""

    def __init__(self, cmd: str, exit_code: int, output: str):
        self.cmd = cmd
        self.exit_code = exit_code
        self.output = output
        super().__init__(f"Command '{cmd}' failed (exit {exit_code}): {output}")
