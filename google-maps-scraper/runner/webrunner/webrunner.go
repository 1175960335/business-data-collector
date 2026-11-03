package webrunner

import (
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gosom/google-maps-scraper/deduper"
	"github.com/gosom/google-maps-scraper/exiter"
	"github.com/gosom/google-maps-scraper/runner"
	"github.com/gosom/google-maps-scraper/tlmt"
	"github.com/gosom/google-maps-scraper/web"
	"github.com/gosom/google-maps-scraper/web/sqlite"
	"github.com/gosom/scrapemate"
	"github.com/gosom/scrapemate/adapters/writers/csvwriter"
	"github.com/gosom/scrapemate/scrapemateapp"
	"golang.org/x/sync/errgroup"
)

type webrunner struct {
	srv       *web.Server
	svc       *web.Service
	cfg       *runner.Config
	store     sqlite.Store
	setupMate func(context.Context, io.Writer, *web.Job) (mateRunner, error)
}

type mateRunner interface {
	Start(context.Context, ...scrapemate.IJob) error
	Close() error
}

// mateStats is implemented by scrapers that can report how many jobs succeeded
// and how many failed. Start returning nil only means the scheduler stopped, so
// without these counters a run where every job failed is indistinguishable from
// a clean one.
type mateStats interface {
	Stats() (completed, failed int64)
}

func New(cfg *runner.Config) (runner.Runner, error) {
	if cfg.DataFolder == "" {
		return nil, fmt.Errorf("data folder is required")
	}

	if err := os.MkdirAll(cfg.DataFolder, os.ModePerm); err != nil {
		return nil, err
	}

	const dbfname = "jobs.db"

	dbpath := filepath.Join(cfg.DataFolder, dbfname)

	repo, err := sqlite.New(dbpath)
	if err != nil {
		return nil, err
	}

	svc := web.NewService(repo, cfg.DataFolder)

	srv, err := web.New(svc, cfg.Addr)
	if err != nil {
		return nil, err
	}

	ans := webrunner{
		srv:       srv,
		svc:       svc,
		cfg:       cfg,
		store:     repo,
		setupMate: defaultSetupMate(cfg),
	}

	return &ans, nil
}

func (w *webrunner) Run(ctx context.Context) error {
	if err := w.recoverInterrupted(ctx); err != nil {
		log.Printf("failed to mark interrupted jobs: %v", err)
	}

	egroup, ctx := errgroup.WithContext(ctx)

	egroup.Go(func() error {
		return w.work(ctx)
	})

	egroup.Go(func() error {
		return w.srv.Start(ctx)
	})

	return egroup.Wait()
}

func (w *webrunner) Close(context.Context) error {
	if w.store == nil {
		return nil
	}

	return w.store.Close()
}

func (w *webrunner) work(ctx context.Context) error {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			jobs, err := w.svc.SelectPending(ctx)
			if err != nil {
				return err
			}

			for i := range jobs {
				select {
				case <-ctx.Done():
					return nil
				default:
					t0 := time.Now().UTC()
					if err := w.scrapeJob(ctx, &jobs[i]); err != nil {
						params := map[string]any{
							"job_count": len(jobs[i].Data.Keywords),
							"duration":  time.Now().UTC().Sub(t0).String(),
							"error":     err.Error(),
						}

						evt := tlmt.NewEvent("web_runner", params)

						_ = runner.Telemetry().Send(ctx, evt)

						log.Printf("error scraping job %s: %v", jobs[i].ID, err)
					} else {
						params := map[string]any{
							"job_count": len(jobs[i].Data.Keywords),
							"duration":  time.Now().UTC().Sub(t0).String(),
						}

						_ = runner.Telemetry().Send(ctx, tlmt.NewEvent("web_runner", params))

						log.Printf("job %s scraped successfully", jobs[i].ID)
					}
				}
			}
		}
	}
}

func (w *webrunner) scrapeJob(ctx context.Context, job *web.Job) error {
	job.Status = web.StatusWorking
	job.Reason = ""
	job.ResultCount = 0

	if err := w.svc.Update(ctx, job); err != nil {
		return err
	}

	if len(job.Data.Keywords) == 0 {
		return w.finish(ctx, job, web.StatusFailed, "no keywords provided")
	}

	outpath := filepath.Join(w.cfg.DataFolder, job.ID+".csv")

	outfile, err := os.Create(outpath)
	if err != nil {
		return w.finish(ctx, job, web.StatusFailed, "cannot create result file: "+err.Error())
	}

	setupMate := w.setupMate
	if setupMate == nil {
		setupMate = defaultSetupMate(w.cfg)
	}

	mate, err := setupMate(ctx, outfile, job)
	if err != nil {
		_ = outfile.Close()

		return w.finish(ctx, job, web.StatusFailed, "cannot start scraper: "+err.Error())
	}

	// Closing twice is not safe for every resource in the chain (the JS fetcher
	// closes a channel), so both closes are funnelled through a guard that runs
	// at most once. The result file must be flushed before it is counted.
	mateClosed := false

	closeMate := func() {
		if mateClosed {
			return
		}

		mateClosed = true

		_ = mate.Close()
		_ = outfile.Close()
	}

	defer closeMate()

	var coords string
	if job.Data.Lat != "" && job.Data.Lon != "" {
		coords = job.Data.Lat + "," + job.Data.Lon
	}

	dedup := deduper.New()
	exitMonitor := exiter.New()

	seedJobs, err := runner.CreateSeedJobs(
		job.Data.FastMode,
		job.Data.Lang,
		strings.NewReader(strings.Join(job.Data.Keywords, "\n")),
		job.Data.Depth,
		job.Data.Email,
		coords,
		job.Data.Zoom,
		func() float64 {
			if job.Data.Radius <= 0 {
				return 10000 // 10 km
			}

			return float64(job.Data.Radius)
		}(),
		dedup,
		exitMonitor,
		w.cfg.ExtraReviews || job.Data.ExtraReviews,
	)
	if err != nil {
		return w.finish(ctx, job, web.StatusFailed, "cannot create seed jobs: "+err.Error())
	}

	if len(seedJobs) == 0 {
		// No work was generated for this query. That is a legitimate outcome
		// and must not be reported as a scrape failure.
		return w.finish(ctx, job, web.StatusOK, "query produced no seed jobs")
	}

	exitMonitor.SetSeedCount(len(seedJobs))

	allowedSeconds := max(60, len(seedJobs)*10*job.Data.Depth/50+120)

	if job.Data.MaxTime > 0 {
		if job.Data.MaxTime.Seconds() < 180 {
			allowedSeconds = 180
		} else {
			allowedSeconds = int(job.Data.MaxTime.Seconds())
		}
	}

	log.Printf("running job %s with %d seed jobs and %d allowed seconds", job.ID, len(seedJobs), allowedSeconds)

	mateCtx, cancel := context.WithTimeout(ctx, time.Duration(allowedSeconds)*time.Second)

	exitMonitor.SetCancelFunc(cancel)

	go exitMonitor.Run(mateCtx)

	runErr := mate.Start(mateCtx, seedJobs...)

	cancel()

	var completed, failed int64

	if stats, ok := mate.(mateStats); ok {
		completed, failed = stats.Stats()
	}

	// Persist a terminal status *before* tearing the scraper down. Close can
	// block on browser teardown, and a blocked Close must not leave the job
	// looking like it is still running. The record count is only known after
	// the writer is flushed, so this status is provisional - but it is always a
	// terminal state, never "working".
	provisional, provisionalReason := provisionalStatus(runErr, completed, failed)

	_ = w.persist(ctx, job, provisional, provisionalReason)

	closeMate()

	records, truncated, countErr := countCSVRecords(outpath)
	if countErr != nil {
		log.Printf("job %s: counting result records failed: %v", job.ID, countErr)
	}

	job.ResultCount = records

	switch {
	case errors.Is(runErr, context.DeadlineExceeded):
		if records > 0 {
			return w.finish(ctx, job, web.StatusPartial,
				fmt.Sprintf("time budget of %ds reached; %d record(s) written so far were kept", allowedSeconds, records))
		}

		return w.finish(ctx, job, web.StatusFailed,
			fmt.Sprintf("time budget of %ds reached with no records", allowedSeconds))

	case errors.Is(runErr, context.Canceled):
		if records > 0 {
			return w.finish(ctx, job, web.StatusPartial, "job was cancelled; partial records were kept")
		}

		return w.finish(ctx, job, web.StatusCancelled, "job was cancelled before producing records")

	case runErr != nil:
		return w.finish(ctx, job, web.StatusFailed, "scrape failed: "+runErr.Error())

	case truncated:
		return w.finish(ctx, job, web.StatusPartial, "result file ends with an incomplete record")

	case failed > 0 && completed == 0:
		return w.finish(ctx, job, web.StatusFailed,
			fmt.Sprintf("all %d scraped job(s) failed and no records were written", failed))

	case failed > 0:
		if records > 0 {
			return w.finish(ctx, job, web.StatusPartial,
				fmt.Sprintf("%d scraped job(s) failed; %d record(s) were still written", failed, records))
		}

		return w.finish(ctx, job, web.StatusFailed,
			fmt.Sprintf("%d scraped job(s) failed and no records were written", failed))

	default:
		// No error, no truncation, no failed jobs. Zero records here is a
		// legitimate empty search, which is not a failure.
		return w.finish(ctx, job, web.StatusOK, "")
	}
}

// provisionalStatus is the best terminal status that can be determined before
// the result file has been flushed and counted. It must never return a
// non-terminal state, so that a Close that blocks forever cannot leave a job
// looking like it is still running.
func provisionalStatus(runErr error, completed, failed int64) (string, string) {
	switch {
	case errors.Is(runErr, context.DeadlineExceeded):
		return web.StatusPartial, "time budget reached; counting records"

	case errors.Is(runErr, context.Canceled):
		return web.StatusCancelled, "job was cancelled; counting records"

	case runErr != nil:
		return web.StatusFailed, "scrape failed: " + runErr.Error()

	case failed > 0 && completed == 0:
		return web.StatusFailed, fmt.Sprintf("all %d scraped job(s) failed", failed)

	default:
		return web.StatusOK, ""
	}
}

// persist writes the job's status and reason, using a context that survives
// cancellation of the parent run.
func (w *webrunner) persist(ctx context.Context, job *web.Job, status, reason string) error {
	job.Status = status
	job.Reason = reason

	// Persist even when the parent context is already cancelled. Otherwise a
	// cancelled or shut-down job would be left in "working" forever, which is
	// exactly the hang this is meant to prevent.
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()

	if err := w.svc.Update(writeCtx, job); err != nil {
		log.Printf("failed to persist status %q for job %s: %v", status, job.ID, err)

		return err
	}

	return nil
}

// finish persists a terminal status and returns a non-nil error for anything
// other than a clean run, so callers surface the outcome instead of reporting
// success.
func (w *webrunner) finish(ctx context.Context, job *web.Job, status, reason string) error {
	if err := w.persist(ctx, job, status, reason); err != nil {
		return fmt.Errorf("persist status %s for job %s: %w", status, job.ID, err)
	}

	log.Printf("job %s finished as %s (records=%d) %s", job.ID, status, job.ResultCount, reason)

	if status == web.StatusOK {
		return nil
	}

	return fmt.Errorf("job %s finished as %s: %s", job.ID, status, reason)
}

// countCSVRecords streams the result file and counts data rows, excluding the
// header. It streams rather than loading the file so large result sets do not
// need to fit in memory. A final record that was cut off mid-write is reported
// through truncated instead of being counted, since counting it would overstate
// the result.
func countCSVRecords(path string) (records int, truncated bool, err error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, false, err
	}

	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return 0, false, err
	}

	if info.Size() == 0 {
		return 0, false, nil
	}

	// csv.Writer always terminates a record with '\n'. A file that does not end
	// with one was interrupted mid-record.
	last := make([]byte, 1)
	if _, err := f.ReadAt(last, info.Size()-1); err != nil {
		return 0, false, err
	}

	truncated = last[0] != '\n'

	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return 0, false, err
	}

	reader := csv.NewReader(f)
	reader.FieldsPerRecord = -1

	first := true

	for {
		if _, readErr := reader.Read(); readErr != nil {
			if errors.Is(readErr, io.EOF) {
				// The reader accepts an unterminated final line as a valid
				// record, but csv.Writer terminates every record with '\n'. A
				// missing trailing newline therefore means the last record was
				// only partially written, so it must not be counted.
				if truncated && records > 0 {
					records--
				}

				return records, truncated, nil
			}

			// Strict quoting is kept on, so an unterminated quoted field shows
			// up here as a parse error and is reported as truncation. The
			// partial record never reaches the counter, so there is nothing to
			// subtract here.
			return records, true, nil
		}

		if first {
			first = false

			continue
		}

		records++
	}
}

// recoverInterrupted marks jobs that were still "working" when a previous
// process died. Without this they would sit in a non-terminal state forever and
// the API would keep reporting them as in progress.
func (w *webrunner) recoverInterrupted(ctx context.Context) error {
	jobs, err := w.svc.SelectByStatus(ctx, web.StatusWorking)
	if err != nil {
		return err
	}

	for i := range jobs {
		jobs[i].Status = web.StatusInterrupted
		jobs[i].Reason = "process restarted while the job was running"

		if err := w.svc.Update(ctx, &jobs[i]); err != nil {
			return err
		}
	}

	if len(jobs) > 0 {
		log.Printf("marked %d interrupted job(s) left over from a previous run", len(jobs))
	}

	return nil
}

func defaultSetupMate(cfg *runner.Config) func(context.Context, io.Writer, *web.Job) (mateRunner, error) {
	return func(_ context.Context, writer io.Writer, job *web.Job) (mateRunner, error) {
		opts := []func(*scrapemateapp.Config) error{
			scrapemateapp.WithConcurrency(cfg.Concurrency),
			scrapemateapp.WithExitOnInactivity(time.Minute * 3),
		}

		if !job.Data.FastMode {
			opts = append(opts,
				scrapemateapp.WithJS(scrapemateapp.DisableImages()),
			)
		} else {
			opts = append(opts,
				scrapemateapp.WithStealth("firefox"),
			)
		}

		opts = runner.AppendBrowserCapacityOptions(opts, cfg)

		hasProxy := false

		if len(cfg.Proxies) > 0 {
			opts = append(opts, scrapemateapp.WithProxies(cfg.Proxies))
			hasProxy = true
		} else if len(job.Data.Proxies) > 0 {
			opts = append(opts,
				scrapemateapp.WithProxies(job.Data.Proxies),
			)
			hasProxy = true
		}

		if !cfg.DisablePageReuse {
			opts = append(opts,
				scrapemateapp.WithPageReuseLimit(2),
				scrapemateapp.WithBrowserReuseLimit(200),
			)
		}

		log.Printf("job %s has proxy: %v", job.ID, hasProxy)

		csvWriter := csvwriter.NewCsvWriter(csv.NewWriter(writer))

		writers := []scrapemate.ResultWriter{csvWriter}

		matecfg, err := scrapemateapp.NewConfig(
			writers,
			opts...,
		)
		if err != nil {
			return nil, err
		}

		return scrapemateapp.NewScrapeMateApp(matecfg)
	}
}
