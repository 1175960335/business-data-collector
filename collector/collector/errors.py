"""Error types.

Every failure the collector can report is a subclass of :class:`CollectorError`,
so the CLI can print one line and return a non-zero exit code without dumping a
traceback on a user who cannot read one.
"""

from __future__ import annotations


class CollectorError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(CollectorError):
    """The configuration file is missing, malformed or semantically invalid.

    The message always names the offending path, e.g.
    ``collect.fields[2]: unknown field 'foo'``.
    """


class SchemaError(CollectorError):
    """The database schema on disk cannot be used by this version."""


class GoInvocationError(CollectorError):
    """The Go scraper could not be started or exited non-zero."""

    def __init__(self, message: str, *, exit_code: int | None = None, stderr_tail: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class ResumeError(CollectorError):
    """A run cannot be resumed, e.g. its configuration fingerprint changed."""


class ExportError(CollectorError):
    """An export could not be written."""
