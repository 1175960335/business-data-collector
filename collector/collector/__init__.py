"""OSINT collector: Python orchestration and persistence over the Go scraper.

Stage C of task book 004. The Go program stays the discovery engine; this package
owns configuration, task/evidence storage, resume and export.

See ``collector/README.md`` for installation and usage.
"""

from .version import EXTRACTOR_VERSION, RAW_SCHEMA, SCHEMA_VERSION, __version__

__all__ = ["__version__", "SCHEMA_VERSION", "EXTRACTOR_VERSION", "RAW_SCHEMA"]
