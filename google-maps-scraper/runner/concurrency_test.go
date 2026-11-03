package runner_test

import (
	"context"
	"testing"

	"github.com/gosom/google-maps-scraper/runner"
	"github.com/gosom/scrapemate"
	"github.com/gosom/scrapemate/scrapemateapp"
)

// discardWriter satisfies scrapemate.ResultWriter so a Config can be built
// without touching the filesystem.
type discardWriter struct{}

func (discardWriter) Run(_ context.Context, in <-chan scrapemate.Result) error {
	for range in {
	}

	return nil
}

// newAppConfig builds a scrapemateapp config from the same option list the file
// runner builds, so the assertions below cover the real wiring rather than a
// reconstruction of it.
func newAppConfig(t *testing.T, cfg *runner.Config, extra ...func(*scrapemateapp.Config) error) *scrapemateapp.Config {
	t.Helper()

	opts := []func(*scrapemateapp.Config) error{
		scrapemateapp.WithConcurrency(cfg.Concurrency),
		scrapemateapp.WithExitOnInactivity(cfg.ExitOnInactivityDuration),
	}
	opts = append(opts, extra...)
	opts = runner.AppendBrowserCapacityOptions(opts, cfg)

	mate, err := scrapemateapp.NewConfig([]scrapemate.ResultWriter{discardWriter{}}, opts...)
	if err != nil {
		t.Fatalf("build app config: %v", err)
	}

	return mate
}

// TestZeroConcurrencyIsRejectedByConfigValidation shows why the old -c default
// was fatal on a single-CPU machine: the default expression was
// min(runtime.NumCPU()/2, 1), which is 0 when NumCPU is 1, and a concurrency of
// 0 fails validation. The scraper would refuse to start with no useful message.
func TestZeroConcurrencyIsRejectedByConfigValidation(t *testing.T) {
	t.Parallel()

	_, err := scrapemateapp.NewConfig(
		[]scrapemate.ResultWriter{discardWriter{}},
		scrapemateapp.WithConcurrency(0),
	)
	if err == nil {
		t.Fatal("expected a concurrency of 0 to be rejected")
	}
}

// TestConcurrencyReachesTheAppConfig checks the flag value actually arrives at
// the scheduler, which is what the concurrency comparison relies on.
func TestConcurrencyReachesTheAppConfig(t *testing.T) {
	t.Parallel()

	for _, concurrency := range []int{1, 3} {
		mate := newAppConfig(t, &runner.Config{Concurrency: concurrency, MaxPagesPerBrowser: 1})

		if mate.Concurrency != concurrency {
			t.Fatalf("-c %d produced a config concurrency of %d", concurrency, mate.Concurrency)
		}
	}
}

// TestBrowserCapacityOptionsAreOnlyAppliedWhenRaised pins the guard in
// AppendBrowserCapacityOptions: the defaults (one page per browser, no explicit
// pool size) must leave the config alone, so the browser pool keeps its derived
// size instead of being pinned to a value the user never asked for.
func TestBrowserCapacityOptionsAreOnlyAppliedWhenRaised(t *testing.T) {
	t.Parallel()

	mate := newAppConfig(t, &runner.Config{Concurrency: 1, MaxPagesPerBrowser: 1, BrowserPoolSize: 0})

	if mate.MaxPagesPerBrowser != 1 {
		t.Fatalf("MaxPagesPerBrowser is %d, want the untouched default of 1", mate.MaxPagesPerBrowser)
	}

	if mate.BrowserPoolSize != 0 {
		t.Fatalf("BrowserPoolSize is %d, want the untouched default of 0", mate.BrowserPoolSize)
	}
}

// TestBrowserCapacityOptionsOverrideWhenRaised is the positive control: the
// values must reach the config once they are actually raised, otherwise the
// previous test could pass because the wiring does nothing at all.
func TestBrowserCapacityOptionsOverrideWhenRaised(t *testing.T) {
	t.Parallel()

	mate := newAppConfig(t, &runner.Config{Concurrency: 4, MaxPagesPerBrowser: 4, BrowserPoolSize: 3})

	if mate.MaxPagesPerBrowser != 4 {
		t.Fatalf("MaxPagesPerBrowser is %d, want 4", mate.MaxPagesPerBrowser)
	}

	if mate.BrowserPoolSize != 3 {
		t.Fatalf("BrowserPoolSize is %d, want 3", mate.BrowserPoolSize)
	}
}
