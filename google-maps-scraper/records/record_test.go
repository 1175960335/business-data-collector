package records_test

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gosom/google-maps-scraper/gmaps"
	"github.com/gosom/google-maps-scraper/records"
	"github.com/gosom/scrapemate"
)

// The two fixtures in testdata/ are extracted verbatim from recorded runs:
//
//	testdata/fast-mode.jsonl   — one line, array framing, 2 records
//	testdata/normal-mode.jsonl — two lines, object framing, 2 records
//
// They are the fixed samples the acceptance criteria ask for. Keeping them as
// real captured payloads rather than hand-written JSON means a change in the
// upstream shape shows up here.
const (
	fastSample   = "testdata/fast-mode.jsonl"
	normalSample = "testdata/normal-mode.jsonl"
)

func readSample(t *testing.T, path string) []records.Record {
	t.Helper()

	f, err := os.Open(filepath.Clean(path))
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}

	defer f.Close()

	var out bytes.Buffer

	stats, err := records.NormalizeJSONL(f, &out, records.Context{Mode: records.ModeNormal})
	if err != nil {
		t.Fatalf("normalise %s: %v", path, err)
	}

	if stats.Malformed != 0 {
		t.Fatalf("%s: %d malformed line(s) in a known-good sample", path, stats.Malformed)
	}

	dec := json.NewDecoder(&out)
	recs := []records.Record{}

	for dec.More() {
		var rec records.Record
		if err := dec.Decode(&rec); err != nil {
			t.Fatalf("decode normalised record: %v", err)
		}

		recs = append(recs, rec)
	}

	if len(recs) != stats.Records {
		t.Fatalf("decoded %d records but stats reported %d", len(recs), stats.Records)
	}

	return recs
}

// TestBothOutputShapesYieldTheSameRecordCount is the core contract: the same
// four businesses arrive as two object lines and one array line, and both must
// produce four records.
func TestBothOutputShapesYieldTheSameRecordCount(t *testing.T) {
	t.Parallel()

	normal := readSample(t, normalSample)
	fast := readSample(t, fastSample)

	if len(normal) != 2 {
		t.Fatalf("normal sample produced %d records, want 2", len(normal))
	}

	if len(fast) != 2 {
		t.Fatalf("fast sample produced %d records, want 2", len(fast))
	}
}

// TestSampleFieldsAreAllKnown pins the schema against the recorded samples. If
// the upstream payload gains a key, this fails and the new key has to be
// acknowledged in KnownFields instead of silently passing through.
func TestSampleFieldsAreAllKnown(t *testing.T) {
	t.Parallel()

	for _, path := range []string{normalSample, fastSample} {
		for _, rec := range readSample(t, path) {
			if len(rec.Unknown) > 0 {
				t.Fatalf("%s: unexpected keys %v", path, rec.Unknown)
			}

			if len(rec.Missing) > 0 {
				t.Fatalf("%s: known keys absent from the sample: %v", path, rec.Missing)
			}

			if len(rec.Raw) != len(records.KnownFields) {
				t.Fatalf("%s: record has %d keys, KnownFields has %d",
					path, len(rec.Raw), len(records.KnownFields))
			}
		}
	}
}

// TestSampleIdentityFieldsSurvive checks the fields later stages merge on.
func TestSampleIdentityFieldsSurvive(t *testing.T) {
	t.Parallel()

	for _, path := range []string{normalSample, fastSample} {
		for i, rec := range readSample(t, path) {
			if got := rec.MissingIdentity(); len(got) > 0 {
				t.Fatalf("%s record %d: missing identity %v", path, i, got)
			}

			if rec.Context.InputID == "" {
				t.Fatalf("%s record %d: input_id was not lifted into the context", path, i)
			}
		}
	}
}

// TestNormalizeJSONLKeepsMalformedLinesFromKillingTheFile covers an interrupted
// write: the corrupt tail must not make the good records unreadable.
func TestNormalizeJSONLKeepsMalformedLinesFromKillingTheFile(t *testing.T) {
	t.Parallel()

	input := strings.Join([]string{
		`{"input_id":"a","title":"Good One"}`, // 1: usable
		`{"input_id":"b","title":`,            // 2: truncated JSON
		`42`,                                  // 3: valid JSON, not a record
		``,                                    // 4: blank, ignored
		`{"input_id":"c","title":"Good Two"}`, // 5: usable
	}, "\n")

	var out bytes.Buffer

	stats, err := records.NormalizeJSONL(strings.NewReader(input), &out, records.Context{})
	if err != nil {
		t.Fatalf("NormalizeJSONL: %v", err)
	}

	if stats.Records != 2 {
		t.Fatalf("records = %d, want 2", stats.Records)
	}

	if stats.Malformed != 2 {
		t.Fatalf("malformed = %d, want 2", stats.Malformed)
	}

	if len(stats.MalformedLines) != 2 || stats.MalformedLines[0] != 2 || stats.MalformedLines[1] != 3 {
		t.Fatalf("malformed lines = %v, want [2 3]", stats.MalformedLines)
	}

	if got := strings.Count(out.String(), "\n"); got != 2 {
		t.Fatalf("wrote %d lines, want 2", got)
	}
}

// TestNormalizeValueRejectsUnattributableValues covers a top-level value that
// cannot belong to any business.
func TestNormalizeValueRejectsUnattributableValues(t *testing.T) {
	t.Parallel()

	for _, v := range []any{float64(42), "a string", true, nil} {
		if _, err := records.NormalizeValue(v, records.Context{}); err == nil {
			t.Fatalf("NormalizeValue(%#v) accepted a value that cannot be a record", v)
		}
	}
}

// TestNormalizeValueCountsNonObjectArrayElements covers a partially valid fast
// mode array: the good elements must survive and the rest must be counted.
func TestNormalizeValueCountsNonObjectArrayElements(t *testing.T) {
	t.Parallel()

	res, err := records.NormalizeValue([]any{
		map[string]any{"input_id": "a", "title": "A"},
		nil,
		float64(7),
		[]any{"nested"},
	}, records.Context{Mode: records.ModeFast})
	if err != nil {
		t.Fatalf("NormalizeValue: %v", err)
	}

	if len(res.Records) != 1 {
		t.Fatalf("records = %d, want 1", len(res.Records))
	}

	if res.Skipped != 3 {
		t.Fatalf("skipped = %d, want 3", res.Skipped)
	}
}

// TestNormalizeObjectReportsDrift covers both directions of drift at once: a
// known key that is gone and a key that was never seen before.
func TestNormalizeObjectReportsDrift(t *testing.T) {
	t.Parallel()

	rec := records.NormalizeObject(map[string]any{
		"input_id":  "abc",
		"title":     "Drifted",
		"brand_new": "unexpected",
		"zeta":      1,
	}, records.Context{})

	if got := strings.Join(rec.Unknown, ","); got != "brand_new,zeta" {
		t.Fatalf("unknown = %q, want sorted \"brand_new,zeta\"", got)
	}

	// Everything except the three keys above is a known key that is absent.
	if len(rec.Missing) != len(records.KnownFields)-2 {
		t.Fatalf("missing = %d entries, want %d", len(rec.Missing), len(records.KnownFields)-2)
	}

	if strings.Join(rec.Missing, ",") != strings.Join(sortedKeys(rec.Missing), ",") {
		t.Fatalf("missing is not sorted: %v", rec.Missing)
	}

	if got := rec.MissingIdentity(); len(got) != 0 {
		t.Fatalf("MissingIdentity = %v, want empty", got)
	}

	// Raw keeps the unknown keys rather than dropping them.
	if rec.Raw["brand_new"] != "unexpected" {
		t.Fatalf("Raw lost an unknown key: %#v", rec.Raw)
	}

	if rec.Schema != records.Schema {
		t.Fatalf("schema = %q, want %q", rec.Schema, records.Schema)
	}

	if rec.Context.Provider != records.ProviderGoogleMaps {
		t.Fatalf("provider = %q, want %q", rec.Context.Provider, records.ProviderGoogleMaps)
	}
}

// TestMissingIdentityTreatsEmptyAsMissing covers the difference between a field
// that is absent and one that is present but blank: both are unusable.
func TestMissingIdentityTreatsEmptyAsMissing(t *testing.T) {
	t.Parallel()

	rec := records.NormalizeObject(map[string]any{
		"input_id": "abc",
		"title":    "",
	}, records.Context{})

	if got := rec.MissingIdentity(); len(got) != 1 || got[0] != "title" {
		t.Fatalf("MissingIdentity = %v, want [title]", got)
	}

	// title is present, so it is not drift.
	if contains(rec.Missing, "title") {
		t.Fatal("a present-but-empty key must not be reported as missing")
	}
}

func sortedKeys(in []string) []string {
	out := append([]string(nil), in...)

	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j] < out[j-1]; j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}

	return out
}

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}

	return false
}

// TestWriterEmitsQueryContextForBothModes is the writer-level contract: the
// same result stream must carry the mode and the query in both modes, which is
// exactly what the raw shapes lose.
func TestWriterEmitsQueryContextForBothModes(t *testing.T) {
	t.Parallel()

	gmapJob := gmaps.NewGmapJob("seed-normal", "en", "coffee in Berlin", 1, false, "52.5,13.4", 15)
	searchJob := gmaps.NewSearchJob(&gmaps.MapSearchParams{
		Location: gmaps.MapLocation{Lat: 52.5, Lon: 13.4, ZoomLvl: 15, Radius: 1000},
		Query:    "coffee in Berlin",
		Hl:       "en",
	})

	cases := []struct {
		name     string
		job      scrapemate.IJob
		data     any
		wantMode string
	}{
		{
			name:     "normal mode object",
			job:      gmapJob,
			data:     &gmaps.Entry{ID: "seed-normal", Title: "Cafe A"},
			wantMode: records.ModeNormal,
		},
		{
			name:     "fast mode array",
			job:      searchJob,
			data:     []*gmaps.Entry{{ID: "seed-fast", Title: "Cafe B"}},
			wantMode: records.ModeFast,
		},
	}

	for _, tc := range cases {
		var buf bytes.Buffer

		w := records.NewWriter(&buf, records.Context{Provider: records.ProviderGoogleMaps})

		ch := make(chan scrapemate.Result, 1)
		ch <- scrapemate.Result{Job: tc.job, Data: tc.data}
		close(ch)

		if err := w.Run(t.Context(), ch); err != nil {
			t.Fatalf("%s: Run: %v", tc.name, err)
		}

		var rec records.Record
		if err := json.Unmarshal(buf.Bytes(), &rec); err != nil {
			t.Fatalf("%s: decode: %v", tc.name, err)
		}

		if rec.Context.Mode != tc.wantMode {
			t.Fatalf("%s: mode = %q, want %q", tc.name, rec.Context.Mode, tc.wantMode)
		}

		if rec.Context.Query != "coffee in Berlin" {
			t.Fatalf("%s: query = %q, want %q", tc.name, rec.Context.Query, "coffee in Berlin")
		}

		if rec.Context.JobID == "" {
			t.Fatalf("%s: job id was not recorded", tc.name)
		}

		if rec.Context.ScrapedAt.IsZero() {
			t.Fatalf("%s: scraped_at was not stamped", tc.name)
		}

		if rec.Context.ScrapedAt.Location() != time.UTC {
			t.Fatalf("%s: scraped_at is not UTC: %v", tc.name, rec.Context.ScrapedAt.Location())
		}

		if rec.Raw["title"] == nil {
			t.Fatalf("%s: raw payload was not preserved: %#v", tc.name, rec.Raw)
		}

		if stats := w.Stats(); stats.Records != 1 {
			t.Fatalf("%s: writer stats records = %d, want 1", tc.name, stats.Records)
		}
	}
}

// TestWriterLiftsInputIDFromPayload covers that the recorded input id is the
// one the writer actually used, not the one the caller guessed.
func TestWriterLiftsInputIDFromPayload(t *testing.T) {
	t.Parallel()

	job := gmaps.NewGmapJob("seed-1", "en", "dentist", 1, false, "", 0)

	var buf bytes.Buffer

	w := records.NewWriter(&buf, records.Context{Provider: records.ProviderGoogleMaps})

	ch := make(chan scrapemate.Result, 1)
	ch <- scrapemate.Result{Job: job, Data: &gmaps.Entry{ID: "resume:abc", Title: "Zahnarzt"}}
	close(ch)

	if err := w.Run(t.Context(), ch); err != nil {
		t.Fatalf("Run: %v", err)
	}

	var rec records.Record
	if err := json.Unmarshal(buf.Bytes(), &rec); err != nil {
		t.Fatalf("decode: %v", err)
	}

	if rec.Context.InputID != "resume:abc" {
		t.Fatalf("input id = %q, want resume:abc", rec.Context.InputID)
	}
}

// TestWriterIgnoresNilData covers a job that produced nothing, which must not
// emit an empty record.
func TestWriterIgnoresNilData(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer

	w := records.NewWriter(&buf, records.Context{})

	ch := make(chan scrapemate.Result, 1)
	ch <- scrapemate.Result{Job: gmaps.NewGmapJob("s", "en", "q", 1, false, "", 0), Data: nil}
	close(ch)

	if err := w.Run(t.Context(), ch); err != nil {
		t.Fatalf("Run: %v", err)
	}

	if buf.Len() != 0 {
		t.Fatalf("expected no output, got %q", buf.String())
	}
}
