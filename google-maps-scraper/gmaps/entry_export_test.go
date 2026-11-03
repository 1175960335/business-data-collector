package gmaps_test

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"strings"
	"testing"

	"github.com/gosom/google-maps-scraper/gmaps"
	"github.com/gosom/scrapemate"
	"github.com/gosom/scrapemate/adapters/writers/csvwriter"
	"github.com/gosom/scrapemate/adapters/writers/jsonwriter"
	"github.com/stretchr/testify/require"
)

// exportFixtures returns entries that exercise the parts of the export contract
// that are easy to get wrong: non-ASCII text, a value containing a comma and a
// quote, a very long value, and a record with almost everything missing.
func exportFixtures() []*gmaps.Entry {
	longDescription := strings.Repeat("Sehr lange Beschreibung mit Umlauten: äöüß. ", 200)

	return []*gmaps.Entry{
		{
			ID:         "input-1",
			Title:      "Zahnärzte Feuerlandhöfe – Dr. Stefan Voß & Kollegen",
			Address:    "Münzstraße 5, 10178 Berlin, Deutschland",
			Category:   "Zahnarzt, Kieferorthopäde",
			WebSite:    "https://example.de/kontakt?q=a,b",
			Phone:      "+493040367600",
			Latitude:   52.5238491,
			Longtitude: 13.4086274,
			PlaceID:    "ChIJm7m4g-1PqEcR9qybv-6X6Ps",
			Emails:     []string{"praxis@triadent-mitte.de", "info@triadent-mitte.de"},
		},
		{
			ID:          "input-1",
			Title:       `Zahnarzt "Mitte" & Partner`,
			Address:     "Hinterhaus, Münzstraße 5",
			Description: longDescription,
			Latitude:    52.5,
			Longtitude:  13.4,
			PlaceID:     "place-with-a-very-long-identifier-0123456789",
			Emails:      []string{"a@b.de"},
		},
		{
			// Almost everything missing: the export must not invent values.
			ID:       "input-1",
			Title:    "Nur ein Name",
			PlaceID:  "only-a-name",
			Latitude: 52.51,
		},
	}
}

// runWriters pushes the fixtures through the real writer adapters the way the
// normal browser path does — one result per place — and returns the CSV and
// JSONL bytes each adapter produced.
//
// The payload type matters: scrapemate's jsonwriter only flattens a []any, and
// wraps anything else as a single value. A browser-mode job returns one
// *gmaps.Entry, so each line is one JSON object. Passing a []*gmaps.Entry here
// would produce one JSON array per line instead, which is the fast-mode framing
// and is covered separately by TestExportArrayFramingForSlicePayload.
func runWriters(t *testing.T, entries []*gmaps.Entry) (csvBytes, jsonlBytes []byte) {
	t.Helper()

	var csvOut, jsonlOut bytes.Buffer

	csvSink := csvwriter.NewCsvWriter(csv.NewWriter(&csvOut))
	jsonSink := jsonwriter.NewJSONWriter(&jsonlOut)

	in := make(chan scrapemate.Result, len(entries))
	for _, entry := range entries {
		in <- scrapemate.Result{Data: entry}
	}

	close(in)

	require.NoError(t, csvSink.Run(context.Background(), in))

	in = make(chan scrapemate.Result, len(entries))
	for _, entry := range entries {
		in <- scrapemate.Result{Data: entry}
	}

	close(in)

	require.NoError(t, jsonSink.Run(context.Background(), in))

	return csvOut.Bytes(), jsonlOut.Bytes()
}

// TestExportCSVRoundTripsUnicodeAndDelimiters checks that a CSV export can be
// read back and that non-ASCII text, embedded commas and embedded quotes survive
// the trip. A record that exports as a different number of fields than the
// header is the classic silent-corruption failure here.
func TestExportCSVRoundTripsUnicodeAndDelimiters(t *testing.T) {
	t.Parallel()

	entries := exportFixtures()
	csvBytes, _ := runWriters(t, entries)

	reader := csv.NewReader(bytes.NewReader(csvBytes))

	header, err := reader.Read()
	require.NoError(t, err)

	rows, err := reader.ReadAll()
	require.NoError(t, err)
	require.Len(t, rows, len(entries))

	for i, row := range rows {
		require.Len(t, row, len(header), "row %d has %d fields but the header has %d", i, len(row), len(header))
	}

	// Column positions come from Entry.CsvHeaders(); resolve them by name so the
	// test does not silently pass if the column order changes.
	index := map[string]int{}
	for i, name := range header {
		index[name] = i
	}

	require.Equal(t, "Zahnärzte Feuerlandhöfe – Dr. Stefan Voß & Kollegen", rows[0][index["title"]])
	require.Equal(t, "Münzstraße 5, 10178 Berlin, Deutschland", rows[0][index["address"]])
	require.Equal(t, `Zahnarzt "Mitte" & Partner`, rows[1][index["title"]])
	require.Equal(t, entries[1].Description, rows[1][index["descriptions"]])
	require.Equal(t, "praxis@triadent-mitte.de, info@triadent-mitte.de", rows[0][index["emails"]])
	require.Equal(t, "input-1", rows[0][index["input_id"]])

	// The sparse record must stay sparse: empty columns, not fabricated values.
	require.Empty(t, rows[2][index["emails"]])
	require.Empty(t, rows[2][index["phone"]])
}

// TestExportCSVAndJSONLAgreeOnRecordsAndIdentities checks the cross-format
// contract: the same input must produce the same number of records with the
// same identities in CSV and in JSONL, so a consumer can switch formats without
// losing or gaining rows.
func TestExportCSVAndJSONLAgreeOnRecordsAndIdentities(t *testing.T) {
	t.Parallel()

	entries := exportFixtures()
	csvBytes, jsonlBytes := runWriters(t, entries)

	csvReader := csv.NewReader(bytes.NewReader(csvBytes))

	csvHeader, err := csvReader.Read()
	require.NoError(t, err)

	csvRows, err := csvReader.ReadAll()
	require.NoError(t, err)

	jsonlRecords := decodeJSONL(t, jsonlBytes)

	require.Len(t, csvRows, len(jsonlRecords), "CSV and JSONL disagree on the record count")

	placeIDColumn := -1
	for i, name := range csvHeader {
		if name == "place_id" {
			placeIDColumn = i
		}
	}

	require.NotEqual(t, -1, placeIDColumn, "the CSV header no longer contains place_id")

	require.Equal(t, identitySet(jsonlRecords, "place_id"), identitySetFromRows(csvRows, placeIDColumn))
}

// TestExportCarriesASourceIdentifierOnEveryRecord pins the only source
// identifier the legacy CSV/JSONL export carries.
//
// Note what this does NOT show: input_id is the seed job's identity, not the
// search query. In browser mode it is a random UUID, under -resume it is a
// sha256 of the query, and in fast mode it is a Google-internal place token.
// The query itself is not present in either format; the osint.record/1 envelope
// (-json-records) adds it as context.query.
func TestExportCarriesASourceIdentifierOnEveryRecord(t *testing.T) {
	t.Parallel()

	entries := exportFixtures()
	_, jsonlBytes := runWriters(t, entries)

	records := decodeJSONL(t, jsonlBytes)
	require.NotEmpty(t, records)

	for i, rec := range records {
		id, _ := rec["input_id"].(string)
		require.NotEmpty(t, id, "record %d has no input_id", i)
	}
}

// TestExportJSONLIsOneObjectPerLine guards the framing the normal browser path
// produces: one JSON object per line, never an array.
func TestExportJSONLIsOneObjectPerLine(t *testing.T) {
	t.Parallel()

	entries := exportFixtures()
	_, jsonlBytes := runWriters(t, entries)

	lines := strings.Split(strings.TrimSpace(string(jsonlBytes)), "\n")
	require.Len(t, lines, len(entries))

	for i, line := range lines {
		require.True(t, strings.HasPrefix(strings.TrimSpace(line), "{"), "line %d is not a JSON object", i)
		require.False(t, strings.HasPrefix(strings.TrimSpace(line), "["), "line %d is a JSON array", i)
	}
}

// TestExportArrayFramingForSlicePayload documents the other framing and why it
// happens.
//
// scrapemate's jsonwriter flattens a []any into one line per element and wraps
// anything else as a single value. A fast-mode search job returns a typed slice
// of entries, so the whole slice is encoded as one value: one JSON array per
// line. That is the fast-mode framing, and it is a consequence of the payload
// type rather than of a flag, so a consumer cannot decide the framing from the
// command line alone — it has to tolerate both.
func TestExportArrayFramingForSlicePayload(t *testing.T) {
	t.Parallel()

	entries := exportFixtures()

	var out bytes.Buffer

	sink := jsonwriter.NewJSONWriter(&out)

	in := make(chan scrapemate.Result, 1)
	in <- scrapemate.Result{Data: entries}

	close(in)

	require.NoError(t, sink.Run(context.Background(), in))

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	require.Len(t, lines, 1, "a typed slice payload must be written as a single line")
	require.True(t, strings.HasPrefix(strings.TrimSpace(lines[0]), "["), "expected a JSON array")

	var decoded []map[string]any
	require.NoError(t, json.Unmarshal([]byte(lines[0]), &decoded))
	require.Len(t, decoded, len(entries))

	// The array framing is what fast mode emits; the record bodies are the same
	// 38-key bodies the object framing emits.
	require.Equal(t, "input-1", decoded[0]["input_id"])
	require.Equal(t, "Zahnärzte Feuerlandhöfe – Dr. Stefan Voß & Kollegen", decoded[0]["title"])
}

// TestExportSlicePayloadStillFlattensPerElement covers the one payload shape
// that is genuinely split across lines, so the distinction above is not read as
// "slices always produce arrays".
func TestExportSlicePayloadStillFlattensPerElement(t *testing.T) {
	t.Parallel()

	entries := exportFixtures()

	payload := make([]any, 0, len(entries))
	for _, e := range entries {
		payload = append(payload, e)
	}

	var out bytes.Buffer

	sink := jsonwriter.NewJSONWriter(&out)

	in := make(chan scrapemate.Result, 1)
	in <- scrapemate.Result{Data: payload}

	close(in)

	require.NoError(t, sink.Run(context.Background(), in))

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	require.Len(t, lines, len(entries))

	for i, line := range lines {
		require.True(t, strings.HasPrefix(strings.TrimSpace(line), "{"), "line %d is not a JSON object", i)
	}
}

func decodeJSONL(t *testing.T, data []byte) []map[string]any {
	t.Helper()

	var records []map[string]any

	dec := json.NewDecoder(bytes.NewReader(data))

	for dec.More() {
		var rec map[string]any
		require.NoError(t, dec.Decode(&rec))

		records = append(records, rec)
	}

	return records
}

func identitySet(records []map[string]any, key string) []string {
	var ids []string

	for _, rec := range records {
		if v, ok := rec[key].(string); ok && v != "" {
			ids = append(ids, v)
		}
	}

	return ids
}

func identitySetFromRows(rows [][]string, column int) []string {
	var ids []string

	for _, row := range rows {
		if column < len(row) && row[column] != "" {
			ids = append(ids, row[column])
		}
	}

	return ids
}
