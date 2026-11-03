"""A stand-in for the Go scraper used by the test suite.

It accepts the argument subset the collector passes and writes ``osint.record/1``
envelopes to ``-results``, so the whole pipeline can be exercised without network
access and without a real scraper build.

Environment controls (all optional):

===================  =========================================================
``STUB_RECORDS``     how many records to emit (default 3)
``STUB_SLEEP``       seconds to sleep before writing anything (default 0)
``STUB_EXIT``        exit code to return (default 0)
``STUB_PARTIAL``     if set, append a truncated final line
``STUB_VARIANT``     string mixed into the payload, so a later run emits
                     different evidence for the same businesses
``STUB_FIXTURE``     path to a JSON file holding a list of raw objects
``STUB_JOB_STATUS``  comma separated per-job statuses written to stderr in the
                     scraper's JSON log shape (e.g. ``failed`` or
                     ``success,failed``); the scraper exits 0 either way
===================  =========================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time

VALUE_FLAGS = {
    "-input",
    "-results",
    "-depth",
    "-c",
    "-lang",
    "-zoom",
    "-exit-on-inactivity",
    "-grid-bbox",
    "-grid-cell",
    "-radius",
    "-proxies",
}


def parse_args(argv: list[str]) -> dict:
    opts: dict = {"depth": "1"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in VALUE_FLAGS:
            opts[token.lstrip("-")] = argv[index + 1] if index + 1 < len(argv) else ""
            index += 2
            continue
        if token.startswith("-"):
            opts[token.lstrip("-")] = True
        index += 1
    return opts


def make_record(index: int, query: str, variant: str) -> dict:
    # The salt makes different queries return different businesses, so a
    # multi-query run produces distinct entities rather than the same three.
    salt = hashlib.sha1(query.encode("utf-8")).hexdigest()[:4]
    return {
        "input_id": query,
        "title": f"Praxis Beispiel {index}{variant}",
        "address": f"Musterstra\u00dfe {index}, 10115 Berlin, Deutschland",
        "phone": f"+49 30 1234{index:04d}",
        "web_site": f"https://praxis-{salt}-{index}.example.de",
        "latitude": 52.52 + index * 0.0001,
        "longitude": 13.40 + index * 0.0001,
        "category": "Zahnarzt",
        "review_rating": 4.5,
        "review_count": 10 + index,
        "data_id": f"0x{salt}{index:04d}:0xabc",
        "link": f"https://www.google.com/maps/place/praxis-{salt}-{index}",
        "emails": [f"praxis{salt}{index}@example.de"],
        "description": "Beispielbeschreibung",
    }


def main(argv: list[str]) -> int:
    opts = parse_args(argv)

    results = opts.get("results")
    if not results or results is True:
        print("stub: -results is required", file=sys.stderr)
        return 2

    query = "unknown"
    input_path = opts.get("input")
    if input_path and input_path is not True:
        try:
            query = pathlib.Path(input_path).read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            pass

    sleep_for = float(os.environ.get("STUB_SLEEP", "0") or 0)
    if sleep_for:
        time.sleep(sleep_for)

    fail_query = os.environ.get("STUB_FAIL_QUERY")
    if fail_query and fail_query in query:
        print(f"stub: failing on purpose for {query!r}", file=sys.stderr)
        return 9

    fixture = os.environ.get("STUB_FIXTURE")
    if fixture:
        raw_records = json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    else:
        count = int(os.environ.get("STUB_RECORDS", "3"))
        variant = os.environ.get("STUB_VARIANT", "")
        raw_records = [make_record(index, query, variant) for index in range(count)]

    mode = "fast" if opts.get("fast-mode") else "normal"

    path = pathlib.Path(results)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for raw in raw_records:
            envelope = {
                "schema": "osint.record/1",
                "context": {
                    "provider": "google_maps",
                    "mode": mode,
                    "input_id": query,
                    "query": query,
                    "source_url": "https://www.google.com/maps/search/" + query.replace(" ", "+"),
                },
                "raw": raw,
                "missing": [],
                "unknown": [],
            }
            handle.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

        if os.environ.get("STUB_PARTIAL"):
            handle.write('{"schema": "osint.record/1", "context": {"provider": "goo')

    print(f"stub: wrote {len(raw_records)} record(s) to {path}")

    # The real scraper exits 0 even when every job failed, and says so only in
    # its stderr JSON log. ``STUB_JOB_STATUS`` reproduces that: a comma
    # separated list of per-job statuses ("failed", "success"), written in the
    # same shape the scraper uses, so the collector's reading of it can be
    # tested without a network outage.
    job_status = os.environ.get("STUB_JOB_STATUS")
    if job_status:
        events = [{"level": "info", "component": "scrapemate", "message": "starting scrapemate"}]
        for status in (part.strip() for part in job_status.split(",")):
            if not status:
                continue
            event = {
                "level": "error" if status == "failed" else "info",
                "component": "scrapemate",
                "status": status,
                "message": "job finished",
            }
            if status == "failed":
                event["error"] = "failed to dial: connection refused"
            events.append(event)
        events.append({"level": "info", "component": "scrapemate", "message": "scrapemate exited"})
        for event in events:
            print(json.dumps(event), file=sys.stderr)

    return int(os.environ.get("STUB_EXIT", "0") or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
