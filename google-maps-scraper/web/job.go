package web

import (
	"context"
	"errors"
	"time"
)

var jobs []Job

const (
	StatusPending = "pending"
	StatusWorking = "working"

	// StatusOK means the run finished cleanly. It does not imply that records
	// were produced: a legitimate query with no matches is still "ok".
	StatusOK = "ok"

	// StatusPartial means the run stopped early (time budget, cancellation or a
	// truncated result file) but the records written so far are usable.
	StatusPartial = "partial"

	StatusFailed = "failed"

	// StatusCancelled means the caller cancelled the job before it produced
	// anything usable.
	StatusCancelled = "cancelled"

	// StatusInterrupted is applied at startup to jobs that were still
	// "working" when the process died, so they never hang in a non-terminal
	// state.
	StatusInterrupted = "interrupted"
)

// IsTerminal reports whether a status is a final state. Non-terminal states
// (pending, working) may still transition; terminal ones must not be silently
// overwritten by a later restart.
func IsTerminal(status string) bool {
	switch status {
	case StatusOK, StatusPartial, StatusFailed, StatusCancelled, StatusInterrupted:
		return true
	default:
		return false
	}
}

// Succeeded reports whether the status represents a usable outcome, i.e. one
// where the produced records can be relied on even if incomplete.
func Succeeded(status string) bool {
	return status == StatusOK || status == StatusPartial
}

type SelectParams struct {
	Status string
	Limit  int
	Offset int
}

// JobPage contains one page of jobs and the metadata needed to navigate it.
type JobPage struct {
	Jobs        []Job
	CurrentPage int
	TotalPages  int
	Total       int
	HasPrev     bool
	HasNext     bool
	PrevPage    int
	NextPage    int
	HasPages    bool
}

type JobRepository interface {
	Get(context.Context, string) (Job, error)
	Create(context.Context, *Job) error
	Delete(context.Context, string) error
	Select(context.Context, SelectParams) ([]Job, error)
	Update(context.Context, *Job) error
}

// Job is one scrape job. The json tags match the published OpenAPI document;
// without them encoding/json would emit the Go field names ("ID", "Name", ...)
// and the documented contract would not describe the real response. The API
// serves JobView, which is the same shape plus the derived max_time_seconds.
type Job struct {
	ID     string    `json:"id"`
	Name   string    `json:"name"`
	Date   time.Time `json:"date"`
	Status string    `json:"status"`
	Data   JobData   `json:"data"`

	// Reason carries the human-readable explanation of a non-ok terminal
	// status. It is empty for a clean run.
	Reason string `json:"reason,omitempty"`

	// ResultCount is the number of data records written to the result file.
	ResultCount int `json:"result_count"`
}

func (j *Job) Validate() error {
	if j.ID == "" {
		return errors.New("missing id")
	}

	if j.Name == "" {
		return errors.New("missing name")
	}

	if j.Status == "" {
		return errors.New("missing status")
	}

	if j.Date.IsZero() {
		return errors.New("missing date")
	}

	if err := j.Data.Validate(); err != nil {
		return err
	}

	return nil
}

type JobData struct {
	Keywords     []string      `json:"keywords"`
	Lang         string        `json:"lang"`
	Zoom         int           `json:"zoom"`
	Lat          string        `json:"lat"`
	Lon          string        `json:"lon"`
	FastMode     bool          `json:"fast_mode"`
	Radius       int           `json:"radius"`
	Depth        int           `json:"depth"`
	Email        bool          `json:"email"`
	ExtraReviews bool          `json:"extra_reviews"`
	MaxTime      time.Duration `json:"max_time"`
	Proxies      []string      `json:"proxies"`
}

func (d *JobData) Validate() error {
	if len(d.Keywords) == 0 {
		return errors.New("missing keywords")
	}

	if d.Lang == "" {
		return errors.New("missing lang")
	}

	if len(d.Lang) != 2 {
		return errors.New("invalid lang")
	}

	if d.Depth == 0 {
		return errors.New("missing depth")
	}

	if d.MaxTime == 0 {
		return errors.New("missing max time")
	}

	if d.FastMode && (d.Lat == "" || d.Lon == "") {
		return errors.New("missing geo coordinates")
	}

	return nil
}
