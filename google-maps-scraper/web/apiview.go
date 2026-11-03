package web

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// The web API used to marshal web.Job directly. Job carries json tags only on
// Reason and ResultCount, so encoding/json fell back to the Go field names for
// the rest and produced "ID", "Name", "Date", "Status", "Data" — mixed with the
// lower-case snake_case keys of the nested JobData. The published OpenAPI
// document (web/static/spec/spec.yaml) has always described lower-case keys, so
// the documented contract and the real response disagreed.
//
// JobView is that documented shape. Job itself now also carries the matching
// tags, so an accidental direct marshal is correct too; a test asserts the two
// stay in step.
//
// Clients written against the old upper-case output keep working through
// legacyJobView, selected with "?format=legacy" or an Accept header of
// application/vnd.gms.legacy+json.

// LegacyFormat selects the pre-fix JSON representation.
const LegacyFormat = "legacy"

// legacyMediaType is the Accept value that selects the pre-fix representation.
const legacyMediaType = "application/vnd.gms.legacy+json"

// JobView is the canonical JSON representation of a Job.
type JobView struct {
	ID          string      `json:"id"`
	Name        string      `json:"name"`
	Date        time.Time   `json:"date"`
	Status      string      `json:"status"`
	Data        JobDataView `json:"data"`
	Reason      string      `json:"reason,omitempty"`
	ResultCount int         `json:"result_count"`
}

// JobDataView is the canonical JSON representation of the scrape parameters.
type JobDataView struct {
	Keywords     []string `json:"keywords"`
	Lang         string   `json:"lang"`
	Zoom         int      `json:"zoom"`
	Lat          string   `json:"lat"`
	Lon          string   `json:"lon"`
	FastMode     bool     `json:"fast_mode"`
	Radius       int      `json:"radius"`
	Depth        int      `json:"depth"`
	Email        bool     `json:"email"`
	ExtraReviews bool     `json:"extra_reviews"`

	// MaxTime is the scrape budget in **nanoseconds**. It is kept in the
	// legacy unit on purpose: existing clients already read this field and
	// silently switching it to seconds would turn a one hour budget into one
	// second. The unit asymmetry is real — the API accepts seconds on input —
	// so new clients should read MaxTimeSeconds instead.
	MaxTime int64 `json:"max_time"`

	// MaxTimeSeconds is the same budget in seconds, which is the unit the
	// create endpoint accepts. Prefer this field.
	MaxTimeSeconds int64 `json:"max_time_seconds"`

	Proxies []string `json:"proxies"`
}

// NewJobView converts a stored job to its documented representation.
func NewJobView(j Job) JobView {
	return JobView{
		ID:          j.ID,
		Name:        j.Name,
		Date:        j.Date,
		Status:      j.Status,
		Data:        NewJobDataView(j.Data),
		Reason:      j.Reason,
		ResultCount: j.ResultCount,
	}
}

// NewJobDataView converts stored parameters to their documented representation.
func NewJobDataView(d JobData) JobDataView {
	return JobDataView{
		Keywords:       d.Keywords,
		Lang:           d.Lang,
		Zoom:           d.Zoom,
		Lat:            d.Lat,
		Lon:            d.Lon,
		FastMode:       d.FastMode,
		Radius:         d.Radius,
		Depth:          d.Depth,
		Email:          d.Email,
		ExtraReviews:   d.ExtraReviews,
		MaxTime:        int64(d.MaxTime),
		MaxTimeSeconds: int64(d.MaxTime / time.Second),
		Proxies:        d.Proxies,
	}
}

// NewJobViews converts a page of jobs.
func NewJobViews(jobs []Job) []JobView {
	out := make([]JobView, 0, len(jobs))

	for i := range jobs {
		out = append(out, NewJobView(jobs[i]))
	}

	return out
}

// legacyJobView reproduces the pre-fix JSON shape byte for byte: upper-case
// outer keys, the nested JobData untouched, and no max_time_seconds.
type legacyJobView struct {
	ID          string
	Name        string
	Date        time.Time
	Status      string
	Data        JobData
	Reason      string `json:"reason,omitempty"`
	ResultCount int    `json:"result_count"`
}

// NewLegacyJobViews converts a page of jobs to the pre-fix representation.
func NewLegacyJobViews(jobs []Job) []legacyJobView {
	out := make([]legacyJobView, 0, len(jobs))

	for i := range jobs {
		out = append(out, legacyJobView{
			ID:          jobs[i].ID,
			Name:        jobs[i].Name,
			Date:        jobs[i].Date,
			Status:      jobs[i].Status,
			Data:        jobs[i].Data,
			Reason:      jobs[i].Reason,
			ResultCount: jobs[i].ResultCount,
		})
	}

	return out
}

// wantsLegacyJobFormat reports whether the caller asked for the pre-fix shape.
func wantsLegacyJobFormat(r *http.Request) bool {
	if r == nil {
		return false
	}

	if strings.EqualFold(strings.TrimSpace(r.URL.Query().Get("format")), LegacyFormat) {
		return true
	}

	return strings.Contains(strings.ToLower(r.Header.Get("Accept")), legacyMediaType)
}

// renderJobsJSON writes a page of jobs in the representation the caller asked
// for.
func renderJobsJSON(w http.ResponseWriter, r *http.Request, code int, jobs []Job) {
	if wantsLegacyJobFormat(r) {
		renderJSON(w, code, NewLegacyJobViews(jobs))

		return
	}

	renderJSON(w, code, NewJobViews(jobs))
}

// renderJobJSON writes a single job in the representation the caller asked for.
func renderJobJSON(w http.ResponseWriter, r *http.Request, code int, job Job) {
	if wantsLegacyJobFormat(r) {
		renderJSON(w, code, NewLegacyJobViews([]Job{job})[0])

		return
	}

	renderJSON(w, code, NewJobView(job))
}

// applyMaxTimeSeconds reconciles the explicit max_time_seconds alias with
// max_time. At this point both fields hold a count of seconds; the caller
// converts to a duration afterwards.
//
// Sending both is allowed only when they agree, so a client cannot silently get
// one value while believing it sent the other.
func applyMaxTimeSeconds(d *JobData, seconds *int64) error {
	if seconds == nil {
		return nil
	}

	if *seconds < 0 {
		return errors.New("max_time_seconds must not be negative")
	}

	if d.MaxTime != 0 && int64(d.MaxTime) != *seconds {
		return fmt.Errorf("max_time (%d) and max_time_seconds (%d) disagree", int64(d.MaxTime), *seconds)
	}

	d.MaxTime = time.Duration(*seconds)

	return nil
}

// validateBudgetSeconds bounds the budget while it is still a count of seconds.
// The handler multiplies by 1e9 to get a duration, which overflows silently for
// absurd input, so the bound has to be applied before that conversion.
func validateBudgetSeconds(seconds time.Duration) error {
	if seconds < 0 {
		return errors.New("max_time must not be negative")
	}

	if int64(seconds) > maxBudgetSeconds {
		return fmt.Errorf("max_time must be at most %d seconds", maxBudgetSeconds)
	}

	return nil
}
