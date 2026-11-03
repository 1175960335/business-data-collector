// Package records defines one transport-neutral record shape for every scraper
// output this repository produces.
//
// Two output shapes exist today and they disagree about framing:
//
//   - normal (browser) mode writes one JSON object per line;
//   - fast mode writes one JSON *array* per query, so a single line can carry
//     many records.
//
// Both shapes carry the same 38 keys per record — verified against the recorded
// samples in verification/fast-mode-raw.jsonl (9 records) and
// verification/browser-mode-raw.jsonl (16 records) — but neither shape records
// which query or which mode produced a record. A downstream reader therefore
// needs mode-specific code and still cannot attribute a record to its query.
//
// A Record wraps the untouched source object in Raw and adds the query context
// the raw shape loses. Raw is never rewritten: absent and unrecognised keys are
// reported in Missing and Unknown instead of being normalised away, so a change
// in the upstream payload stays visible rather than being silently absorbed.
package records

import (
	"fmt"
	"sort"
	"time"
)

// Schema identifies the envelope version. It changes whenever the meaning of a
// Record field changes, so consumers can reject a stream they do not
// understand.
const Schema = "osint.record/1"

// Output modes.
const (
	// ModeNormal is the browser mode: one object per line.
	ModeNormal = "normal"
	// ModeFast is the reduced fast mode: one array per query.
	ModeFast = "fast"
)

// ProviderGoogleMaps is the provider id used for records produced by the
// Google Maps scraper.
const ProviderGoogleMaps = "google_maps"

// KnownFields is the set of keys observed in both output modes. It is used to
// report drift, not to filter: unknown keys are preserved in Record.Raw.
//
// "longtitude" is included deliberately. The upstream struct field is
// misspelled and both spellings are emitted for backward compatibility; see
// gmaps.Entry.MarshalJSON. It is a known key, not an unknown one.
var KnownFields = map[string]struct{}{
	"about":                 {},
	"address":               {},
	"categories":            {},
	"category":              {},
	"cid":                   {},
	"complete_address":      {},
	"credit_cards_accepted": {},
	"data_id":               {},
	"description":           {},
	"emails":                {},
	"images":                {},
	"input_id":              {},
	"latitude":              {},
	"link":                  {},
	"longitude":             {},
	"longtitude":            {},
	"menu":                  {},
	"open_hours":            {},
	"order_online":          {},
	"owner":                 {},
	"phone":                 {},
	"place_id":              {},
	"plus_code":             {},
	"popular_times":         {},
	"price_range":           {},
	"reservations":          {},
	"review_count":          {},
	"review_rating":         {},
	"reviews_link":          {},
	"reviews_per_rating":    {},
	"status":                {},
	"street_view_url":       {},
	"thumbnail":             {},
	"timezone":              {},
	"title":                 {},
	"user_reviews":          {},
	"user_reviews_extended": {},
	"web_site":              {},
}

// IdentityFields must be present and non-empty for a record to be attributable
// to a business. A record missing either one cannot be merged or de-duplicated
// later, so its absence is worth surfacing rather than filling in.
var IdentityFields = []string{"input_id", "title"}

// Context records where a record came from. Without it a fast-mode array line
// is indistinguishable from a normal-mode object line, and no record can be
// traced back to the query that produced it.
type Context struct {
	// Provider identifies the source system, e.g. ProviderGoogleMaps.
	Provider string `json:"provider"`

	// Mode is ModeNormal or ModeFast. It is recorded rather than inferred so a
	// consumer can tell which framing the record arrived in.
	Mode string `json:"mode"`

	// InputID is the caller-supplied seed id, when the source exposes it. It is
	// normally also present in Raw.
	InputID string `json:"input_id,omitempty"`

	// JobID is the scraper job that produced the record.
	JobID string `json:"job_id,omitempty"`

	// Query is the search text. Fast mode carries it as the "q" URL parameter;
	// normal mode embeds it in the maps URL path.
	Query string `json:"query,omitempty"`

	// SourceURL is the URL that was fetched.
	SourceURL string `json:"source_url,omitempty"`

	// ScrapedAt is when the record was written. Zero means unknown.
	ScrapedAt time.Time `json:"scraped_at,omitempty"`
}

// Record is the common shape written for both output modes.
type Record struct {
	Schema  string         `json:"schema"`
	Context Context        `json:"context"`
	Raw     map[string]any `json:"raw"`

	// Missing lists KnownFields that the source object did not contain at all.
	Missing []string `json:"missing,omitempty"`

	// Unknown lists keys the source object contained that this schema version
	// does not recognise. They are still present in Raw.
	Unknown []string `json:"unknown,omitempty"`
}

// NormalizeResult is the outcome of normalising one decoded JSON value.
type NormalizeResult struct {
	// Records are the records extracted from the value.
	Records []Record

	// Skipped counts array elements that were not JSON objects, e.g. nulls or
	// nested arrays. They cannot become records and are dropped, but they are
	// counted so a shape change is visible instead of silent.
	Skipped int
}

// NormalizeValue turns a decoded JSON value into records.
//
// An object becomes one record; an array becomes one record per object element.
// Any other top-level value is an error, because it cannot be attributed to a
// business at all.
func NormalizeValue(v any, ctx Context) (NormalizeResult, error) {
	switch typed := v.(type) {
	case map[string]any:
		return NormalizeResult{Records: []Record{NormalizeObject(typed, ctx)}}, nil

	case []any:
		var out NormalizeResult

		for _, item := range typed {
			obj, ok := item.(map[string]any)
			if !ok {
				out.Skipped++

				continue
			}

			out.Records = append(out.Records, NormalizeObject(obj, ctx))
		}

		return out, nil

	default:
		return NormalizeResult{}, fmt.Errorf("records: cannot normalise %T: expected a JSON object or array", v)
	}
}

// NormalizeObject wraps one source object in the common envelope.
//
// The returned record shares the caller's map; it is not copied, because Raw is
// documented as the untouched source object.
func NormalizeObject(obj map[string]any, ctx Context) Record {
	rec := Record{
		Schema:  Schema,
		Context: ctx,
		Raw:     obj,
	}

	if rec.Context.Provider == "" {
		rec.Context.Provider = ProviderGoogleMaps
	}

	// An input_id inside the payload is more trustworthy than the one the
	// caller guessed, because it is the id the writer actually used.
	if id, ok := obj["input_id"].(string); ok && id != "" {
		rec.Context.InputID = id
	}

	for key := range obj {
		if _, known := KnownFields[key]; !known {
			rec.Unknown = append(rec.Unknown, key)
		}
	}

	for key := range KnownFields {
		if _, present := obj[key]; !present {
			rec.Missing = append(rec.Missing, key)
		}
	}

	sort.Strings(rec.Unknown)
	sort.Strings(rec.Missing)

	return rec
}

// MissingIdentity returns the identity fields that are absent or empty, i.e.
// the reasons this record cannot be attributed to a business. It is separate
// from Missing, which reports schema drift across all known fields.
func (r Record) MissingIdentity() []string {
	var out []string

	for _, key := range IdentityFields {
		value, present := r.Raw[key]
		if !present || isEmptyValue(value) {
			out = append(out, key)
		}
	}

	return out
}

// isEmptyValue reports whether a decoded JSON value carries no usable content.
// An explicit null, an empty string and an empty container all mean "no value",
// which is not the same as "field absent" but is equally unusable.
func isEmptyValue(v any) bool {
	switch typed := v.(type) {
	case nil:
		return true
	case string:
		return typed == ""
	case []any:
		return len(typed) == 0
	case map[string]any:
		return len(typed) == 0
	default:
		return false
	}
}
