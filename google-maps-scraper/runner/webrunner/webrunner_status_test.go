//nolint:testpackage // These tests need the unexported setupMate hook to avoid running a browser.
package webrunner

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gosom/google-maps-scraper/runner"
	"github.com/gosom/google-maps-scraper/web"
	"github.com/gosom/scrapemate"
)

// stubMate is a scraper stand-in that can succeed, fail, time out, be
// cancelled, report job counters, and write a controlled CSV payload.
type stubMate struct {
	w         io.Writer
	csv       string
	startErr  error
	completed int64
	failed    int64
	onClose   func()
}

func (m *stubMate) Start(context.Context, ...scrapemate.IJob) error {
	if m.w != nil && m.csv != "" {
		if _, err := io.WriteString(m.w, m.csv); err != nil {
			return err
		}
	}

	return m.startErr
}

func (m *stubMate) Close() error {
	if m.onClose != nil {
		m.onClose()
	}

	return nil
}

func (m *stubMate) Stats() (int64, int64) {
	return m.completed, m.failed
}

func newStatusRunner(t *testing.T, stub *stubMate) (*webrunner, *web.Service) {
	t.Helper()

	folder := t.TempDir()

	repo := &memoryJobRepo{}
	svc := web.NewService(repo, folder)

	w := &webrunner{
		svc: svc,
		cfg: &runner.Config{DataFolder: folder, Concurrency: 1},
		setupMate: func(_ context.Context, wr io.Writer, _ *web.Job) (mateRunner, error) {
			stub.w = wr

			return stub, nil
		},
	}

	return w, svc
}

func newStatusJob(t *testing.T, svc *web.Service) web.Job {
	t.Helper()

	job := web.Job{
		ID:     "job-status",
		Name:   "coffee",
		Date:   time.Now().UTC(),
		Status: web.StatusPending,
		Data: web.JobData{
			Keywords: []string{"coffee"},
			Lang:     "en",
			Zoom:     15,
			Lat:      "37.7749",
			Lon:      "-122.4194",
			FastMode: true,
			Radius:   1000,
			Depth:    10,
			MaxTime:  time.Minute,
		},
	}

	if err := svc.Create(context.Background(), &job); err != nil {
		t.Fatalf("create job: %v", err)
	}

	return job
}

const testCSVHeader = "name,phone\n"

func statusAfterRun(t *testing.T, w *webrunner, svc *web.Service, job *web.Job) web.Job {
	t.Helper()

	_ = w.scrapeJob(context.Background(), job)

	got, err := svc.Get(context.Background(), job.ID)
	if err != nil {
		t.Fatalf("get job: %v", err)
	}

	return got
}

// TestScrapeJobFailedWhenScraperCannotStart covers a startup failure.
func TestScrapeJobFailedWhenScraperCannotStart(t *testing.T) {
	t.Parallel()

	folder := t.TempDir()

	repo := &memoryJobRepo{}
	svc := web.NewService(repo, folder)

	w := &webrunner{
		svc: svc,
		cfg: &runner.Config{DataFolder: folder, Concurrency: 1},
		setupMate: func(context.Context, io.Writer, *web.Job) (mateRunner, error) {
			return nil, errors.New("browser launch failed")
		},
	}

	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusFailed {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusFailed)
	}

	if !strings.Contains(got.Reason, "cannot start scraper") || !strings.Contains(got.Reason, "browser launch failed") {
		t.Fatalf("reason %q does not explain the startup failure", got.Reason)
	}
}

// TestScrapeJobFailedWhenStartReturnsError covers a request-level failure.
func TestScrapeJobFailedWhenStartReturnsError(t *testing.T) {
	t.Parallel()

	stub := &stubMate{startErr: errors.New("connection refused")}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusFailed {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusFailed)
	}

	if !strings.Contains(got.Reason, "connection refused") {
		t.Fatalf("reason %q does not carry the underlying error", got.Reason)
	}
}

// TestScrapeJobPartialWhenTimeoutWithRecords is the "timeout but data exists"
// case: the records are kept and the job must not be reported as ok.
func TestScrapeJobPartialWhenTimeoutWithRecords(t *testing.T) {
	t.Parallel()

	stub := &stubMate{
		csv:      testCSVHeader + "a,1\nb,2\nc,3\n",
		startErr: context.DeadlineExceeded,
	}

	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusPartial {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusPartial)
	}

	if got.ResultCount != 3 {
		t.Fatalf("result count = %d, want 3", got.ResultCount)
	}

	if !strings.Contains(got.Reason, "time budget") {
		t.Fatalf("reason %q does not mention the time budget", got.Reason)
	}
}

// TestScrapeJobFailedWhenTimeoutWithoutRecords makes sure a timeout that
// produced nothing is not silently reported as a usable result.
func TestScrapeJobFailedWhenTimeoutWithoutRecords(t *testing.T) {
	t.Parallel()

	stub := &stubMate{startErr: context.DeadlineExceeded}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusFailed {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusFailed)
	}

	if got.ResultCount != 0 {
		t.Fatalf("result count = %d, want 0", got.ResultCount)
	}
}

// TestScrapeJobCancelledWithoutRecords covers cancellation before any result.
func TestScrapeJobCancelledWithoutRecords(t *testing.T) {
	t.Parallel()

	stub := &stubMate{startErr: context.Canceled}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusCancelled {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusCancelled)
	}
}

// TestScrapeJobPartialOnCancelWithRecords keeps usable records after a cancel.
func TestScrapeJobPartialOnCancelWithRecords(t *testing.T) {
	t.Parallel()

	stub := &stubMate{csv: testCSVHeader + "a,1\n", startErr: context.Canceled}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusPartial {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusPartial)
	}

	if got.ResultCount != 1 {
		t.Fatalf("result count = %d, want 1", got.ResultCount)
	}
}

// TestScrapeJobOKForLegitimateEmptySearch is the counterpart: a query with no
// matches is a success, not a failure, and must be distinguishable from one.
func TestScrapeJobOKForLegitimateEmptySearch(t *testing.T) {
	t.Parallel()

	stub := &stubMate{}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	_ = w.scrapeJob(context.Background(), &job)

	got, err := svc.Get(context.Background(), job.ID)
	if err != nil {
		t.Fatalf("get job: %v", err)
	}

	if got.Status != web.StatusOK {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusOK)
	}

	if got.ResultCount != 0 {
		t.Fatalf("result count = %d, want 0", got.ResultCount)
	}

	if got.Reason != "" {
		t.Fatalf("expected no reason for a clean run, got %q", got.Reason)
	}
}

// TestScrapeJobFailedWhenAllScrapedJobsFailed is the regression for "exit code
// 0 and status ok while nothing was scraped": Start returns nil, but every job
// failed, so the run must be reported as failed.
func TestScrapeJobFailedWhenAllScrapedJobsFailed(t *testing.T) {
	t.Parallel()

	stub := &stubMate{completed: 0, failed: 5}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusFailed {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusFailed)
	}

	if !strings.Contains(got.Reason, "5") {
		t.Fatalf("reason %q does not report the failed job count", got.Reason)
	}
}

// TestScrapeJobPartialWhenSomeScrapedJobsFailed keeps usable records while
// still surfacing that part of the run failed.
func TestScrapeJobPartialWhenSomeScrapedJobsFailed(t *testing.T) {
	t.Parallel()

	stub := &stubMate{
		csv:       testCSVHeader + "a,1\nb,2\n",
		completed: 3,
		failed:    2,
	}

	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusPartial {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusPartial)
	}

	if got.ResultCount != 2 {
		t.Fatalf("result count = %d, want 2", got.ResultCount)
	}
}

// TestScrapeJobPartialOnTruncatedResultFile covers a writer that was cut off
// mid-record: the complete records are kept, the partial one is not counted.
func TestScrapeJobPartialOnTruncatedResultFile(t *testing.T) {
	t.Parallel()

	stub := &stubMate{csv: testCSVHeader + "a,1\nb,2\nincomplete"}
	w, svc := newStatusRunner(t, stub)
	job := newStatusJob(t, svc)

	got := statusAfterRun(t, w, svc, &job)

	if got.Status != web.StatusPartial {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusPartial)
	}

	if got.ResultCount != 2 {
		t.Fatalf("result count = %d, want 2 (the truncated row must not be counted)", got.ResultCount)
	}
}

// TestRecoverInterruptedMarksLeftoverWorkingJobs covers restart recovery: a job
// left in a non-terminal state by a previous process must not hang forever.
func TestRecoverInterruptedMarksLeftoverWorkingJobs(t *testing.T) {
	t.Parallel()

	stub := &stubMate{}
	w, svc := newStatusRunner(t, stub)

	job := newStatusJob(t, svc)

	job.Status = web.StatusWorking
	if err := svc.Update(context.Background(), &job); err != nil {
		t.Fatalf("update job: %v", err)
	}

	if err := w.recoverInterrupted(context.Background()); err != nil {
		t.Fatalf("recoverInterrupted: %v", err)
	}

	got, err := svc.Get(context.Background(), job.ID)
	if err != nil {
		t.Fatalf("get job: %v", err)
	}

	if got.Status != web.StatusInterrupted {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusInterrupted)
	}

	if got.Reason == "" {
		t.Fatal("expected a reason explaining the interruption")
	}
}

// ---------------------------------------------------------------------------
// countCSVRecords
// ---------------------------------------------------------------------------

func writeTempCSV(t *testing.T, content string) string {
	t.Helper()

	path := filepath.Join(t.TempDir(), "out.csv")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write csv: %v", err)
	}

	return path
}

// TestCountCSVRecordsHandlesQuotesAndUnicode checks that embedded newlines
// inside quoted fields do not inflate the count and that non-ASCII survives.
func TestCountCSVRecordsHandlesQuotesAndUnicode(t *testing.T) {
	t.Parallel()

	content := "name,address\n" +
		"\"Café Zürich\",\"Straße 1,\n10707 Berlin\"\n" +
		"\"Zahnarzt \"\"Mitte\"\"\",\"Torstraße 2\"\n" +
		"日本語クリニック,東京都\n"

	records, truncated, err := countCSVRecords(writeTempCSV(t, content))
	if err != nil {
		t.Fatalf("countCSVRecords: %v", err)
	}

	if records != 3 {
		t.Fatalf("records = %d, want 3", records)
	}

	if truncated {
		t.Fatal("a complete file must not be reported as truncated")
	}
}

// TestCountCSVRecordsDetectsTruncatedFile covers a file whose last record was
// cut off mid-write.
func TestCountCSVRecordsDetectsTruncatedFile(t *testing.T) {
	t.Parallel()

	content := "name,phone\na,1\nb,2\nc,3"

	records, truncated, err := countCSVRecords(writeTempCSV(t, content))
	if err != nil {
		t.Fatalf("countCSVRecords: %v", err)
	}

	if !truncated {
		t.Fatal("expected a file without a trailing newline to be flagged as truncated")
	}

	if records != 2 {
		t.Fatalf("records = %d, want 2 (the incomplete row must not be counted)", records)
	}
}

// TestCountCSVRecordsEmptyFile covers a job that produced nothing at all.
func TestCountCSVRecordsEmptyFile(t *testing.T) {
	t.Parallel()

	records, truncated, err := countCSVRecords(writeTempCSV(t, ""))
	if err != nil {
		t.Fatalf("countCSVRecords: %v", err)
	}

	if records != 0 || truncated {
		t.Fatalf("records = %d, truncated = %v; want 0, false", records, truncated)
	}
}

// TestCountCSVRecordsHeaderOnly covers a search that legitimately matched
// nothing: the header alone is not a record.
func TestCountCSVRecordsHeaderOnly(t *testing.T) {
	t.Parallel()

	records, truncated, err := countCSVRecords(writeTempCSV(t, testCSVHeader))
	if err != nil {
		t.Fatalf("countCSVRecords: %v", err)
	}

	if records != 0 || truncated {
		t.Fatalf("records = %d, truncated = %v; want 0, false", records, truncated)
	}
}

// TestCountCSVRecordsTruncatedHeaderOnly covers the floor case: an unterminated
// header has no complete record to keep, and the counter must not go negative.
func TestCountCSVRecordsTruncatedHeaderOnly(t *testing.T) {
	t.Parallel()

	records, truncated, err := countCSVRecords(writeTempCSV(t, "name,phone"))
	if err != nil {
		t.Fatalf("countCSVRecords: %v", err)
	}

	if records != 0 {
		t.Fatalf("records = %d, want 0 (the header is not a record and must not go negative)", records)
	}

	if !truncated {
		t.Fatal("expected an unterminated file to be flagged as truncated")
	}
}

// TestCountCSVRecordsTruncatedKeepsCompleteRows covers the mixed case: complete
// records before the cut are kept, only the partial tail is dropped.
func TestCountCSVRecordsTruncatedKeepsCompleteRows(t *testing.T) {
	t.Parallel()

	content := "name,phone\na,1\nb,2\nc,3\nparti"

	records, truncated, err := countCSVRecords(writeTempCSV(t, content))
	if err != nil {
		t.Fatalf("countCSVRecords: %v", err)
	}

	if !truncated {
		t.Fatal("expected the file to be flagged as truncated")
	}

	if records != 3 {
		t.Fatalf("records = %d, want 3 (a,1 b,2 c,3 are complete; \"parti\" is not)", records)
	}
}

// TestProvisionalStatusIsAlwaysTerminal guards the invariant that a blocked
// Close can never leave a job looking like it is still running.
func TestProvisionalStatusIsAlwaysTerminal(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name      string
		runErr    error
		completed int64
		failed    int64
	}{
		{name: "clean", runErr: nil},
		{name: "deadline", runErr: context.DeadlineExceeded},
		{name: "cancelled", runErr: context.Canceled},
		{name: "failed", runErr: errors.New("boom")},
		{name: "all jobs failed", runErr: nil, failed: 3},
	}

	for _, tc := range cases {
		status, _ := provisionalStatus(tc.runErr, tc.completed, tc.failed)

		if !web.IsTerminal(status) {
			t.Fatalf("%s: provisional status %q is not terminal", tc.name, status)
		}
	}
}
