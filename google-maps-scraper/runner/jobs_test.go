package runner_test

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"testing"

	"github.com/gosom/google-maps-scraper/deduper"
	"github.com/gosom/google-maps-scraper/gmaps"
	"github.com/gosom/google-maps-scraper/grid"
	"github.com/gosom/google-maps-scraper/runner"
)

func TestCreateGridSeedJobsRejectsInvalidZoom(t *testing.T) {
	t.Parallel()

	bbox := grid.BoundingBox{
		MinLat: 40.30,
		MinLon: -3.80,
		MaxLat: 40.50,
		MaxLon: -3.60,
	}

	_, err := runner.CreateGridSeedJobs(
		"en",
		strings.NewReader("coffee"),
		10,
		false,
		bbox,
		1.0,
		0,
		nil,
		nil,
		false,
	)
	if err == nil || !strings.Contains(err.Error(), "invalid zoom level") {
		t.Fatalf("expected invalid zoom level error, got %v", err)
	}
}

func TestCreateSeedJobsRejectsEmptyQueryBeforeCustomID(t *testing.T) {
	t.Parallel()

	_, err := runner.CreateSeedJobs(
		false,
		"en",
		strings.NewReader("  #!#my-id\n"),
		10,
		false,
		"",
		15,
		10000,
		nil,
		nil,
		false,
	)
	if err == nil || !strings.Contains(err.Error(), "empty query text") {
		t.Fatalf("expected empty query text error, got %v", err)
	}
}

func TestCreateGridSeedJobsRejectsEmptyQueryBeforeCustomID(t *testing.T) {
	t.Parallel()

	bbox := grid.BoundingBox{
		MinLat: 40.30,
		MinLon: -3.80,
		MaxLat: 40.50,
		MaxLon: -3.60,
	}

	_, err := runner.CreateGridSeedJobs(
		"en",
		strings.NewReader(" #!#my-id\n"),
		10,
		false,
		bbox,
		1.0,
		15,
		nil,
		nil,
		false,
	)
	if err == nil || !strings.Contains(err.Error(), "empty query text") {
		t.Fatalf("expected empty query text error, got %v", err)
	}
}

func TestCreateSeedJobsSkipsCompletedInputs(t *testing.T) {
	t.Parallel()

	jobs, err := runner.CreateSeedJobs(
		false,
		"en",
		strings.NewReader("coffee\ntea\n"),
		10,
		false,
		"",
		15,
		10000,
		nil,
		nil,
		false,
		runner.WithDeterministicSeedIDs(),
		runner.WithCompletedInputSkipper(func(inputID string) bool {
			return strings.HasPrefix(inputID, "resume:")
		}),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(jobs) != 0 {
		t.Fatalf("expected completed deterministic inputs to be skipped, got %d jobs", len(jobs))
	}
}

func TestCreateSeedJobsAddsCompletionTracker(t *testing.T) {
	t.Parallel()

	tracker := &recordingSeedTracker{}

	jobs, err := runner.CreateSeedJobs(
		false,
		"en",
		strings.NewReader("coffee\n"),
		10,
		false,
		"",
		15,
		10000,
		nil,
		nil,
		false,
		runner.WithCompletionTracker(tracker),
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	job, ok := jobs[0].(*gmaps.GmapJob)
	if !ok {
		t.Fatalf("expected *gmaps.GmapJob, got %T", jobs[0])
	}

	if job.CompletionTracker != tracker {
		t.Fatalf("completion tracker was not attached")
	}
}

type recordingSeedTracker struct{}

func (t *recordingSeedTracker) SeedDiscovered(string, int) error {
	return nil
}

// berlinMitteBBox is the small bounding box used by the grid tests. With 0.5 km
// cells it produces 16 cells, small enough to reason about by hand.
var berlinMitteBBox = grid.BoundingBox{
	MinLat: 52.515,
	MinLon: 13.385,
	MaxLat: 52.535,
	MaxLon: 13.415,
}

// cellCenterOf extracts the cell centre that CreateGridSeedJobs encoded into a
// GmapJob URL, which has the shape
// https://www.google.com/maps/search/<query>/@<lat>,<lon><zoom>z
func cellCenterOf(t *testing.T, job *gmaps.GmapJob) (lat, lon float64) {
	t.Helper()

	_, after, ok := strings.Cut(job.URL, "/@")
	if !ok {
		t.Fatalf("job URL %q carries no cell centre", job.URL)
	}

	parts := strings.Split(after, ",")
	if len(parts) != 3 {
		t.Fatalf("job URL %q has an unexpected cell centre suffix", job.URL)
	}

	lat, err := strconv.ParseFloat(parts[0], 64)
	if err != nil {
		t.Fatalf("cell latitude in %q is not a number: %v", job.URL, err)
	}

	lon, err = strconv.ParseFloat(parts[1], 64)
	if err != nil {
		t.Fatalf("cell longitude in %q is not a number: %v", job.URL, err)
	}

	return lat, lon
}

// TestCreateGridSeedJobsProducesOneJobPerQueryAndCell pins the fan-out: every
// query is run once per cell, and every cell gets its own identity so the
// results of one cell cannot be mistaken for another's.
func TestCreateGridSeedJobsProducesOneJobPerQueryAndCell(t *testing.T) {
	t.Parallel()

	const (
		cellSizeKm = 0.5
		zoom       = 15
	)

	cellCount := len(grid.GenerateCells(berlinMitteBBox, cellSizeKm))
	if cellCount != 16 {
		t.Fatalf("test fixture expects 16 cells, generated %d", cellCount)
	}

	jobs, err := runner.CreateGridSeedJobs(
		"en",
		strings.NewReader("dentists in Berlin\nZahnarzt Berlin\n"),
		1,
		false,
		berlinMitteBBox,
		cellSizeKm,
		zoom,
		nil,
		nil,
		false,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(jobs) != 2*cellCount {
		t.Fatalf("expected %d jobs for 2 queries across %d cells, got %d", 2*cellCount, cellCount, len(jobs))
	}

	ids := make(map[string]struct{}, len(jobs))
	centers := make(map[string]struct{}, len(jobs))

	for _, raw := range jobs {
		job, ok := raw.(*gmaps.GmapJob)
		if !ok {
			t.Fatalf("expected *gmaps.GmapJob, got %T", raw)
		}

		if _, dup := ids[job.ID]; dup {
			t.Fatalf("cell id %q was reused across jobs", job.ID)
		}

		ids[job.ID] = struct{}{}

		lat, lon := cellCenterOf(t, job)
		centers[fmt.Sprintf("%f,%f", lat, lon)] = struct{}{}
	}

	if len(centers) != cellCount {
		t.Fatalf("expected %d distinct cell centres, got %d", cellCount, len(centers))
	}
}

// TestCreateGridSeedJobsCentersAreInsideBBox checks that the generated cell
// centres really fall inside the requested box, so a caller asking for an area
// is not silently given searches somewhere else.
func TestCreateGridSeedJobsCentersAreInsideBBox(t *testing.T) {
	t.Parallel()

	jobs, err := runner.CreateGridSeedJobs(
		"en",
		strings.NewReader("dentists in Berlin\n"),
		1,
		false,
		berlinMitteBBox,
		0.5,
		15,
		nil,
		nil,
		false,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	for _, raw := range jobs {
		job, ok := raw.(*gmaps.GmapJob)
		if !ok {
			t.Fatalf("expected *gmaps.GmapJob, got %T", raw)
		}

		lat, lon := cellCenterOf(t, job)

		if !berlinMitteBBox.Contains(lat, lon) {
			t.Fatalf("cell centre %f,%f of job %s is outside the requested box", lat, lon, job.ID)
		}
	}
}

// TestCreateGridSeedJobsSharesOneDeduperAcrossCells is the overlap-dedup
// guarantee. Adjacent cells routinely discover the same place, so the deduper
// must be shared by every cell job; a per-cell deduper would let the same place
// through once per cell.
func TestCreateGridSeedJobsSharesOneDeduperAcrossCells(t *testing.T) {
	t.Parallel()

	shared := deduper.New()

	jobs, err := runner.CreateGridSeedJobs(
		"en",
		strings.NewReader("dentists in Berlin\n"),
		1,
		false,
		berlinMitteBBox,
		0.5,
		15,
		shared,
		nil,
		false,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(jobs) == 0 {
		t.Fatal("expected jobs")
	}

	for _, raw := range jobs {
		job, ok := raw.(*gmaps.GmapJob)
		if !ok {
			t.Fatalf("expected *gmaps.GmapJob, got %T", raw)
		}

		if job.Deduper != shared {
			t.Fatalf("job %s does not share the grid deduper", job.ID)
		}
	}

	// The behaviour the sharing buys: the first cell to see a place enqueues it,
	// the next cell to see the same place does not.
	ctx := context.Background()
	place := "https://www.google.com/maps/place/Example/data=!4m2!3m1!1s0x0:0x0"

	if !shared.AddIfNotExists(ctx, place) {
		t.Fatal("first discovery of a place should be enqueued")
	}

	if shared.AddIfNotExists(ctx, place) {
		t.Fatal("second discovery of the same place should be deduplicated")
	}
}

// TestCreateGridSeedJobsDeterministicIDsCoverEveryCell checks the resume
// contract for grid runs: the identity of a cell is derived from the query and
// the cell centre, so a re-run produces the same IDs and completed cells can be
// skipped instead of re-scraped.
func TestCreateGridSeedJobsDeterministicIDsCoverEveryCell(t *testing.T) {
	t.Parallel()

	create := func(opts ...runner.SeedJobOption) map[string]struct{} {
		jobs, err := runner.CreateGridSeedJobs(
			"en",
			strings.NewReader("dentists in Berlin\n"),
			1,
			false,
			berlinMitteBBox,
			0.5,
			15,
			nil,
			nil,
			false,
			opts...,
		)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		ids := make(map[string]struct{}, len(jobs))

		for _, raw := range jobs {
			job, ok := raw.(*gmaps.GmapJob)
			if !ok {
				t.Fatalf("expected *gmaps.GmapJob, got %T", raw)
			}

			if !strings.HasPrefix(job.ID, "resume:") {
				t.Fatalf("expected a deterministic resume id, got %q", job.ID)
			}

			ids[job.ID] = struct{}{}
		}

		return ids
	}

	first := create(runner.WithDeterministicSeedIDs())
	second := create(runner.WithDeterministicSeedIDs())

	if len(first) != 16 {
		t.Fatalf("expected 16 deterministic cell ids, got %d", len(first))
	}

	if len(second) != len(first) {
		t.Fatalf("second run produced %d ids, first produced %d", len(second), len(first))
	}

	for id := range first {
		if _, ok := second[id]; !ok {
			t.Fatalf("cell id %s is not stable across runs", id)
		}
	}

	// A skipper that has already completed one cell must remove exactly that one.
	var skipped string
	for id := range first {
		skipped = id
		break
	}

	remaining := create(
		runner.WithDeterministicSeedIDs(),
		runner.WithCompletedInputSkipper(func(inputID string) bool { return inputID == skipped }),
	)

	if len(remaining) != len(first)-1 {
		t.Fatalf("expected %d remaining cells after skipping one, got %d", len(first)-1, len(remaining))
	}

	if _, ok := remaining[skipped]; ok {
		t.Fatalf("skipped cell %s was still scheduled", skipped)
	}
}

// TestCreateGridSeedJobsRejectsDegenerateGrids covers the two ways a grid run
// can be asked for nothing. Both must fail loudly rather than start a run that
// quietly collects no data.
func TestCreateGridSeedJobsRejectsDegenerateGrids(t *testing.T) {
	t.Parallel()

	t.Run("box narrower than half a cell", func(t *testing.T) {
		t.Parallel()

		tiny := grid.BoundingBox{MinLat: 52.5, MinLon: 13.4, MaxLat: 52.5001, MaxLon: 13.4001}

		_, err := runner.CreateGridSeedJobs(
			"en", strings.NewReader("dentists in Berlin\n"), 1, false, tiny, 1.0, 15, nil, nil, false,
		)
		if err == nil || !strings.Contains(err.Error(), "grid produced 0 cells") {
			t.Fatalf("expected a 0 cells error, got %v", err)
		}
	})

	t.Run("no queries", func(t *testing.T) {
		t.Parallel()

		_, err := runner.CreateGridSeedJobs(
			"en", strings.NewReader("\n\n"), 1, false, berlinMitteBBox, 0.5, 15, nil, nil, false,
		)
		if err == nil || !strings.Contains(err.Error(), "no queries found") {
			t.Fatalf("expected a no queries error, got %v", err)
		}
	})
}

// TestCreateGridSeedJobsPropagatesEmailOption checks the email flag reaches the
// cell jobs; without it the grid run would collect places but no addresses.
func TestCreateGridSeedJobsPropagatesEmailOption(t *testing.T) {
	t.Parallel()

	jobs, err := runner.CreateGridSeedJobs(
		"en", strings.NewReader("dentists in Berlin\n"), 1, true, berlinMitteBBox, 1.0, 15, nil, nil, false,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	for _, raw := range jobs {
		job, ok := raw.(*gmaps.GmapJob)
		if !ok {
			t.Fatalf("expected *gmaps.GmapJob, got %T", raw)
		}

		if !job.ExtractEmail {
			t.Fatalf("job %s did not inherit the email extraction flag", job.ID)
		}
	}
}
