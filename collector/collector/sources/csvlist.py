"""The CSV / URL list provider: a file the operator already has.

Stage E asks for "用户 URL/CSV 列表" as one of the sources. This provider is
deliberately the simplest of the four: it reads a file, touches no network, and
is therefore the one source whose behaviour is fully deterministic and can be
tested end to end without a stub server. That makes it the control case in the
acceptance run: when a real source fails, this one still produces its rows, which
is exactly the "一个来源失败仍产出其他来源的部分结果" check.

Nothing is inferred. A missing column yields a missing fact with a reason, the
same contract as every other source.
"""

from __future__ import annotations

import csv
import pathlib

from .base import (
    ACQUIRED_VIA,
    PROVENANCE_KEYS,
    Provider,
    ProviderOutcome,
    ProviderTask,
    RunContext,
    SourceRecord,
    write_envelopes,
)

#: Canonical fields this provider can fill.
#:
#: ``description`` was added in round 024: a directory's listing/summary export
#: often carries the business's own scope, and the industry match (021 A) reads
#: the *description* — without this a CSV of such rows could only prove an
#: industry from the company name, which the task book forbids.
FIELDS = (
    "title", "address", "phone", "website", "emails", "latitude", "longitude",
    "category", "description",
)

#: Column names recognised per canonical field, most specific first. An operator
#: mapping in the config takes precedence over this list, so a file with
#: unconventional headers still works without code changes.
CANONICAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "title": ("title", "name", "company", "business", "firma", "betrieb"),
    "address": ("address", "addr", "street", "adresse", "anschrift"),
    "phone": ("phone", "tel", "telephone", "telefon"),
    "website": ("website", "web", "url", "homepage", "site", "webseite"),
    "emails": ("emails", "email", "e-mail", "mail"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng", "long"),
    "category": ("category", "industry", "branche", "kategorie", "rubrik", "type"),
    "description": (
        "description", "desc", "profile", "scope", "summary",
        "主营产品", "经营范围", "主营业务", "公司简介",
    ),
}


class CsvListProvider(Provider):
    """Discovery from a local CSV (or TSV / plain text) file."""

    name = "csv_list"
    title = "User-supplied CSV / URL list (local file)"
    license = "whatever licence covers the operator's own file"
    attribution = (
        "Records come from a file the operator supplied. The collector does not "
        "add rights to it and does not alter the values."
    )
    requires_account = False
    provenance = ""

    # -- configuration -------------------------------------------------------

    @property
    def settings(self) -> dict:
        node = (self.cfg.data.get("sources_map") or {}).get(self.name) or {}
        return node if isinstance(node, dict) else {}

    def path(self) -> str:
        return str(self.settings.get("path") or "").strip()

    def id_column(self) -> str:
        return str(self.settings.get("id_column") or "").strip()

    def column_map(self) -> dict[str, str]:
        """Operator-configured column -> canonical field, validated."""
        raw = self.settings.get("columns") or {}
        mapping: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                column = str(key).strip()
                target = str(value).strip()
                if column and target in FIELDS:
                    mapping[column] = target
        return mapping

    def provenance_settings(self) -> dict:
        """The operator's ``provenance`` block for this file, validated shallowly."""
        node = self.settings.get("provenance") or {}
        return node if isinstance(node, dict) else {}

    def provenance_defaults(self) -> dict[str, str]:
        """File-wide provenance, applied to every row.

        ``acquired_via`` defaults to ``import`` and never to ``live``: a CSV
        provider reads a file, so nothing it produces may read as a page the
        collector visited. A value outside the vocabulary falls back to
        ``import`` rather than being trusted.
        """
        node = self.provenance_settings()
        out: dict[str, str] = {}
        for key in PROVENANCE_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
        acquired = out.get("acquired_via", "")
        out["acquired_via"] = acquired if acquired in ACQUIRED_VIA else "import"
        return out

    def provenance_columns(self) -> dict[str, str]:
        """Per-row provenance: CSV column -> provenance key.

        Provenance is not a business field, so it gets its own mapping rather
        than being mixed into ``columns`` (which only targets canonical fields).
        """
        raw = self.provenance_settings().get("columns") or {}
        mapping: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                column = str(key).strip()
                target = str(value).strip()
                if column and target in PROVENANCE_KEYS:
                    mapping[column] = target
        return mapping

    def is_configured(self) -> bool:
        if self.name not in (self.cfg.data.get("sources") or []):
            return False
        return bool(self.path())

    def not_configured_reason(self) -> str:
        if self.name not in (self.cfg.data.get("sources") or []):
            return "csv_list is not listed in sources"
        return "csv_list needs sources_map.csv_list.path"

    # -- planning ------------------------------------------------------------

    def tasks(self) -> list[ProviderTask]:
        path = self.path()
        return [
            ProviderTask(
                label=f"discover-csv-{_slug(pathlib.Path(path).stem or 'list')}",
                query=path,
                targets=(path,),
                extra={"path": path},
            )
        ]

    # -- execution -----------------------------------------------------------

    def run_task(self, task: ProviderTask, ctx: RunContext) -> ProviderOutcome:
        limit = ctx.budget_reason()
        if limit:
            return ProviderOutcome(ok=False, skipped_reason=limit)

        ctx.spend()
        source = pathlib.Path(task.extra.get("path") or self.path())
        if not source.is_file():
            return ProviderOutcome(
                ok=False,
                error=f"the configured file does not exist: {source}",
                error_kind="missing_file",
            )

        try:
            records = self._read(source)
        except OSError as exc:
            return ProviderOutcome(
                ok=False,
                error=f"could not read {source}: {exc}",
                error_kind="read_failed",
            )

        raw_path = ctx.raw_path(task.label, task.attempt)
        written = write_envelopes(
            raw_path,
            self.name,
            records,
            query=str(source),
            context={
                # The transport: the local file the rows were read out of. The
                # *source site* travels on each record's provenance, so evidence
                # can cite the real page without this path being mistaken for it.
                "source_url": str(source),
                "input_path": str(source),
                "mode": "csv_file",
                "csv_rows": len(records),
            },
        )
        return ProviderOutcome(ok=True, records=written, raw_path=raw_path)

    def _read(self, source: pathlib.Path) -> list[SourceRecord]:
        delimiter = "\t" if str(self.settings.get("delimiter") or "").strip() == "tab" else ","
        mapping = self.column_map()
        id_column = self.id_column()

        out: list[SourceRecord] = []
        with open(source, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            # A single-column file ("name\nPraxis Nord\n") has no delimiter to
            # find, and ``Sniffer`` raises rather than defaulting. Left
            # unguarded, that exception escapes ``run_task`` and takes the whole
            # run down instead of yielding the rows the file does have.
            sniffed: str | None = None
            if sample.strip():
                try:
                    sniffed = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
                except csv.Error:
                    sniffed = None
            reader = csv.reader(
                handle,
                delimiter=delimiter if sniffed is None else sniffed,
            )
            header: list[str] = []
            for row in reader:
                if not row or all(not cell.strip() for cell in row):
                    continue
                if not header:
                    header = [cell.strip() for cell in row]
                    continue
                # Headers are matched case-insensitively. A German CSV writes
                # every noun capitalised ("Firma", "Anschrift", "Telefon",
                # "Webseite", "E-Mail"), and with a case-sensitive lookup every
                # one of those misses the canonical column list, so the row has
                # no title and is dropped as "not a business" — a whole file
                # silently yielding zero records.
                values = {
                    name.casefold(): (row[index].strip() if index < len(row) else "")
                    for index, name in enumerate(header)
                }
                record = self._record(values, mapping, id_column, len(out) + 1, source, header)
                if record is not None:
                    out.append(record)
        return out

    def _record(
        self,
        values: dict[str, str],
        mapping: dict[str, str],
        id_column: str,
        row_number: int,
        source: pathlib.Path,
        header: list[str] | None = None,
    ) -> SourceRecord | None:
        picked = self._pick(values, mapping)

        title = picked.get("title")
        if not title:
            # A row without a name cannot be attributed to a business and must
            # not be invented. The caller reports the skipped row.
            return None

        source_id = values.get((id_column or "").casefold().strip(), "") if id_column else ""
        if not source_id:
            source_id = f"row-{row_number}"

        emails = tuple(
            part.strip()
            for part in _split(picked.get("emails", ""), ",;|")
            if part.strip()
        )

        # ``native`` keeps the operator's own header spelling, so the evidence is
        # what the file actually said rather than a normalised echo of it.
        native = {
            name: values.get(name.casefold(), "")
            for name in (header if header is not None else values)
        }

        prov = self._provenance(values)

        return SourceRecord(
            title=title,
            address=picked.get("address") or None,
            phone=picked.get("phone") or None,
            website=picked.get("website") or None,
            emails=emails,
            latitude=_to_float(picked.get("latitude")),
            longitude=_to_float(picked.get("longitude")),
            category=picked.get("category") or None,
            description=picked.get("description") or None,
            source_id=source_id,
            source_id_kind="csv_row",
            native=native,
            extra={"row": row_number, "file": source.name},
            acquired_via=prov["acquired_via"],
            source_page_url=prov.get("source_page_url"),
            detail_url=prov.get("detail_url"),
            # A CSV row never proves the detail page was visited: the file may
            # have been exported long before, and the collector never opened it.
            detail_fetched=False,
            origin=prov.get("origin"),
            snapshot_id=prov.get("snapshot_id"),
            observed_at=prov.get("observed_at"),
        )

    def _provenance(self, values: dict[str, str]) -> dict[str, str]:
        """File-wide provenance, with any per-row column value overriding it.

        A row that does not carry the column keeps the file-wide default, so a
        single-page export needs no per-row columns and a mixed export can stamp
        each row with its own page.
        """
        prov = self.provenance_defaults()
        for column, key in self.provenance_columns().items():
            text = values.get(column.casefold(), "")
            if text:
                prov[key] = text
        return prov

    @staticmethod
    def _pick(values: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
        """One value per canonical field.

        An operator-mapped column wins over a guess, and the recognised column
        names are tried in a fixed order, so a file carrying both "name" and
        "company" resolves the same way on every run. Matching is
        case-insensitive, because the canonical names are lowercase while real
        headers are not.
        """
        picked: dict[str, str] = {}
        for target, defaults in CANONICAL_COLUMNS.items():
            configured = [
                column.casefold().strip()
                for column, mapped in mapping.items()
                if mapped == target and column
            ]
            for column in configured + [c for c in defaults if c not in configured]:
                text = values.get(column, "")
                if text:
                    picked[target] = text
                    break
        return picked


def _split(text: str, separators: str) -> list[str]:
    for separator in separators:
        text = text.replace(separator, "\x00")
    return text.split("\x00")


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _slug(text: str, limit: int = 40) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return (cleaned or "list")[:limit]
