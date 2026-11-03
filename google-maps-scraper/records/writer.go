package records

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"
	"time"

	"github.com/gosom/google-maps-scraper/gmaps"
	"github.com/gosom/scrapemate"
)

var _ scrapemate.ResultWriter = (*Writer)(nil)

// Writer is a scrapemate.ResultWriter that emits Records instead of the raw
// payload, so normal mode and fast mode produce the same stream and every
// record carries the query that produced it.
//
// It deliberately re-encodes the payload through JSON before normalising it.
// That guarantees Raw is byte-for-byte what the standard JSON writer would have
// written, including gmaps.Entry's dual longitude keys, instead of a second,
// subtly different rendering of the same struct.
type Writer struct {
	enc       *json.Encoder
	clock     func() time.Time
	stats     WriterStats
	jobCtx    Context
	scrapedAt time.Time
}

// WriterStats reports what a Writer saw. Malformed is not tracked here because
// the payload is produced in-process from typed structs; it is only reachable
// when reading a file that another process wrote, which is NormalizeJSONL's job.
type WriterStats struct {
	Records int
	Skipped int
	Unknown int
	Missing int
}

// NewWriter wraps w. The returned writer records the given context on every
// record, filling in the per-job fields as results arrive.
func NewWriter(w io.Writer, ctx Context) *Writer {
	if ctx.Provider == "" {
		ctx.Provider = ProviderGoogleMaps
	}

	return &Writer{
		enc:    json.NewEncoder(w),
		clock:  time.Now,
		jobCtx: ctx,
	}
}

// Stats returns the counters accumulated so far.
func (x *Writer) Stats() WriterStats {
	return x.stats
}

// Run implements scrapemate.ResultWriter.
func (x *Writer) Run(_ context.Context, in <-chan scrapemate.Result) error {
	for result := range in {
		if result.Data == nil {
			continue
		}

		payload, err := json.Marshal(result.Data)
		if err != nil {
			return fmt.Errorf("records: marshal result for job %s: %w", jobID(result.Job), err)
		}

		var decoded any
		if err := json.Unmarshal(payload, &decoded); err != nil {
			return fmt.Errorf("records: decode result for job %s: %w", jobID(result.Job), err)
		}

		ctx := x.contextFor(result.Job)

		res, err := NormalizeValue(decoded, ctx)
		if err != nil {
			return fmt.Errorf("records: normalise result for job %s: %w", jobID(result.Job), err)
		}

		x.stats.Skipped += res.Skipped

		for i := range res.Records {
			if len(res.Records[i].Unknown) > 0 {
				x.stats.Unknown++
			}

			if len(res.Records[i].Missing) > 0 {
				x.stats.Missing++
			}

			if err := x.enc.Encode(res.Records[i]); err != nil {
				return fmt.Errorf("records: write record for job %s: %w", jobID(result.Job), err)
			}

			x.stats.Records++
		}
	}

	return nil
}

// contextFor derives the per-job part of the envelope.
func (x *Writer) contextFor(job scrapemate.IJob) Context {
	ctx := x.jobCtx
	ctx.Mode = modeOf(job)

	if ctx.JobID == "" {
		ctx.JobID = jobID(job)
	}

	if ctx.Query == "" {
		ctx.Query = queryFromJob(job)
	}

	if ctx.SourceURL == "" && job != nil {
		ctx.SourceURL = job.GetURL()
	}

	ctx.ScrapedAt = x.clock().UTC()

	return ctx
}

func jobID(job scrapemate.IJob) string {
	if job == nil {
		return ""
	}

	return job.GetID()
}

// modeOf reports which framing a job's results use. Fast mode is the only mode
// that sends the query as a "q" URL parameter, which is a reliable fallback for
// job types this package does not know about.
func modeOf(job scrapemate.IJob) string {
	switch job.(type) {
	case *gmaps.SearchJob:
		return ModeFast
	case *gmaps.GmapJob:
		return ModeNormal
	}

	if job != nil && job.GetURLParams()["q"] != "" {
		return ModeFast
	}

	return ModeNormal
}

// queryFromJob recovers the search text. Fast mode carries it in the "q" URL
// parameter; normal mode embeds it in the maps URL path.
func queryFromJob(job scrapemate.IJob) string {
	if job == nil {
		return ""
	}

	if q := job.GetURLParams()["q"]; q != "" {
		return q
	}

	const marker = "/maps/search/"

	raw := job.GetURL()

	idx := strings.Index(raw, marker)
	if idx < 0 {
		return ""
	}

	rest := raw[idx+len(marker):]

	// The query ends where the "@lat,lon,zoom" suffix or the query string
	// begins.
	if cut := strings.IndexAny(rest, "/?"); cut >= 0 {
		rest = rest[:cut]
	}

	unescaped, err := url.QueryUnescape(rest)
	if err != nil {
		return rest
	}

	return unescaped
}
