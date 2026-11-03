//nolint:testpackage // tests the private SQLite repository and schema directly
package sqlite

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite" // sqlite driver

	"github.com/gosom/google-maps-scraper/web"
)

// legacySchema is the jobs table as it existed before reason/result_count were
// added. Migration must keep databases created by that version readable.
const legacySchema = `
	CREATE TABLE IF NOT EXISTS jobs (
		id TEXT PRIMARY KEY,
		name TEXT NOT NULL,
		status TEXT NOT NULL,
		data TEXT NOT NULL,
		created_at INT NOT NULL,
		updated_at INT NOT NULL
	);
`

func createLegacyDatabase(t *testing.T) string {
	t.Helper()

	path := filepath.Join(t.TempDir(), "jobs.db")

	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open legacy db: %v", err)
	}

	defer db.Close()

	if _, err := db.Exec(legacySchema); err != nil {
		t.Fatalf("create legacy schema: %v", err)
	}

	_, err = db.Exec(
		`INSERT INTO jobs (id, name, status, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)`,
		"legacy-1", "legacy job", web.StatusOK, `{"keywords":["coffee"],"lang":"en","depth":1,"max_time":180000000000}`,
		time.Now().UTC().Unix(), time.Now().UTC().Unix(),
	)
	if err != nil {
		t.Fatalf("insert legacy row: %v", err)
	}

	return path
}

// openStore opens a repository and guarantees the database handle is released
// before the test's temporary directory is removed. t.Cleanup runs LIFO, so
// this close happens before t.TempDir's own cleanup.
func openStore(t *testing.T, path string) Store {
	t.Helper()

	repo, err := New(path)
	if err != nil {
		t.Fatalf("open db: %v", err)
	}

	t.Cleanup(func() {
		if err := repo.Close(); err != nil {
			t.Errorf("close db: %v", err)
		}
	})

	return repo
}

// TestMigrationAddsStatusColumnsToLegacyDatabase is the regression for opening
// a pre-existing jobs.db: CREATE TABLE IF NOT EXISTS does not add columns, so
// without an explicit migration every query against the new column list fails.
func TestMigrationAddsStatusColumnsToLegacyDatabase(t *testing.T) {
	t.Parallel()

	path := createLegacyDatabase(t)

	repo := openStore(t, path)

	got, err := repo.Get(context.Background(), "legacy-1")
	if err != nil {
		t.Fatalf("get legacy job after migration: %v", err)
	}

	if got.ID != "legacy-1" {
		t.Fatalf("id = %q, want legacy-1", got.ID)
	}

	// The migrated columns default to empty/zero rather than breaking the scan.
	if got.Reason != "" {
		t.Fatalf("reason = %q, want empty", got.Reason)
	}

	if got.ResultCount != 0 {
		t.Fatalf("result count = %d, want 0", got.ResultCount)
	}
}

// TestReasonAndResultCountRoundTrip checks the new fields survive storage.
func TestReasonAndResultCountRoundTrip(t *testing.T) {
	t.Parallel()

	path := createLegacyDatabase(t)

	repo := openStore(t, path)

	job := web.Job{
		ID:          "job-2",
		Name:        "partial job",
		Date:        time.Now().UTC().Truncate(time.Second),
		Status:      web.StatusPartial,
		Reason:      "time budget reached; 12 record(s) written so far were kept",
		ResultCount: 12,
		Data: web.JobData{
			Keywords: []string{"coffee"},
			Lang:     "en",
			Depth:    1,
			MaxTime:  time.Minute,
		},
	}

	if err := repo.Create(context.Background(), &job); err != nil {
		t.Fatalf("create job: %v", err)
	}

	got, err := repo.Get(context.Background(), "job-2")
	if err != nil {
		t.Fatalf("get job: %v", err)
	}

	if got.Status != web.StatusPartial {
		t.Fatalf("status = %q, want %q", got.Status, web.StatusPartial)
	}

	if got.ResultCount != 12 {
		t.Fatalf("result count = %d, want 12", got.ResultCount)
	}

	if got.Reason != job.Reason {
		t.Fatalf("reason = %q, want %q", got.Reason, job.Reason)
	}

	// Update must persist the new columns too.
	got.ResultCount = 20
	got.Status = web.StatusOK
	got.Reason = ""

	if err := repo.Update(context.Background(), &got); err != nil {
		t.Fatalf("update job: %v", err)
	}

	again, err := repo.Get(context.Background(), "job-2")
	if err != nil {
		t.Fatalf("get updated job: %v", err)
	}

	if again.ResultCount != 20 || again.Status != web.StatusOK || again.Reason != "" {
		t.Fatalf("update did not round-trip: count=%d status=%q reason=%q",
			again.ResultCount, again.Status, again.Reason)
	}
}

// TestMigrationIsIdempotent ensures repeated startups do not fail.
func TestMigrationIsIdempotent(t *testing.T) {
	t.Parallel()

	path := createLegacyDatabase(t)

	for i := range 3 {
		repo, err := New(path)
		if err != nil {
			t.Fatalf("open #%d failed: %v", i+1, err)
		}

		if err := repo.Close(); err != nil {
			t.Fatalf("close #%d failed: %v", i+1, err)
		}
	}
}

// TestSelectByStatusFiltersOnNewStatuses confirms the new terminal statuses are
// queryable, which startup recovery relies on.
func TestSelectByStatusFiltersOnNewStatuses(t *testing.T) {
	t.Parallel()

	path := createLegacyDatabase(t)

	repo := openStore(t, path)

	job := web.Job{
		ID:     "job-working",
		Name:   "leftover",
		Date:   time.Now().UTC().Truncate(time.Second),
		Status: web.StatusWorking,
		Data: web.JobData{
			Keywords: []string{"coffee"},
			Lang:     "en",
			Depth:    1,
			MaxTime:  time.Minute,
		},
	}

	if err := repo.Create(context.Background(), &job); err != nil {
		t.Fatalf("create job: %v", err)
	}

	jobs, err := repo.Select(context.Background(), web.SelectParams{Status: web.StatusWorking})
	if err != nil {
		t.Fatalf("select: %v", err)
	}

	if len(jobs) != 1 || jobs[0].ID != "job-working" {
		t.Fatalf("expected the working job, got %v", jobs)
	}

	if web.IsTerminal(web.StatusWorking) {
		t.Fatal("working must not be a terminal status")
	}

	for _, s := range []string{web.StatusOK, web.StatusPartial, web.StatusFailed, web.StatusCancelled, web.StatusInterrupted} {
		if !web.IsTerminal(s) {
			t.Fatalf("%q must be terminal", s)
		}
	}
}

// TestCloseReleasesDatabaseFile is the regression for a Windows-only defect:
// the database runs in WAL mode, so an unreleased handle keeps jobs.db and its
// -wal/-shm sidecars locked and the data directory cannot be removed. This
// broke test cleanup and would equally block archiving or deleting a data
// folder in production.
func TestCloseReleasesDatabaseFile(t *testing.T) {
	t.Parallel()

	dir := filepath.Join(t.TempDir(), "data")

	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	repo, err := New(filepath.Join(dir, "jobs.db"))
	if err != nil {
		t.Fatalf("open db: %v", err)
	}

	if err := repo.Close(); err != nil {
		t.Fatalf("close db: %v", err)
	}

	if err := os.RemoveAll(dir); err != nil {
		t.Fatalf("data directory still locked after Close: %v", err)
	}

	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("data directory still present after RemoveAll: %v", err)
	}
}
