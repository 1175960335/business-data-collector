"""Version constants.

``SCHEMA_VERSION`` is the SQLite schema generation. It is bumped only when the
meaning of an existing column changes; adding a table or column is handled by
``db.ensure_schema`` without a bump.

Stage D added two columns to ``field_facts`` (``validation_state``,
``checks_json``) additively, so the generation stays at 1: a database written by
the stage C collector still reads correctly, and opening it applies the two
``ALTER TABLE`` statements in ``db.ADDITIVE_COLUMNS``.
"""

__version__ = "0.2.0"

#: SQLite schema generation, stored in ``PRAGMA user_version``.
SCHEMA_VERSION = 1

#: Identifies the code that turned a provider payload into field facts. Stored
#: on every fact so a later extractor change is visible in the data.
EXTRACTOR_VERSION = "gmaps-entry/1"

#: Envelope schema this collector understands when reading raw files.
RAW_SCHEMA = "osint.record/1"
