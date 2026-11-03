package jshttp

import (
	"strings"
	"testing"
)

// TestChromiumLaunchArgsAvoidSingleProcess is the regression guard for the
// crash that produced `playwright: target closed` during navigation.
//
// Upstream v1.4.0 passed --single-process, which on Windows kills the whole
// browser on real HTTPS sites (measured with tools/browserprobe: example.com
// survives, google.com / bing.com / Google Maps do not). Re-adding the flag
// would silently reintroduce a failure that only shows up against live sites,
// so it is pinned here rather than left to integration testing.
func TestChromiumLaunchArgsAvoidSingleProcess(t *testing.T) {
	t.Parallel()

	for _, arg := range chromiumLaunchArgs {
		if arg == "--single-process" {
			t.Fatal("--single-process must not be passed to Chromium: it crashes the browser on real HTTPS sites")
		}

		if strings.HasPrefix(arg, "--single-process=") {
			t.Fatalf("--single-process must not be passed to Chromium, found %q", arg)
		}
	}
}

// TestChromiumLaunchArgsAreWellFormed keeps the extracted list tidy: no empty
// entries, no exact duplicates, every entry a flag.
func TestChromiumLaunchArgsAreWellFormed(t *testing.T) {
	t.Parallel()

	seen := make(map[string]struct{}, len(chromiumLaunchArgs))

	for _, arg := range chromiumLaunchArgs {
		if arg == "" {
			t.Fatal("chromiumLaunchArgs contains an empty entry")
		}

		if !strings.HasPrefix(arg, "--") {
			t.Fatalf("expected every launch argument to be a flag, got %q", arg)
		}

		if _, dup := seen[arg]; dup {
			t.Fatalf("chromiumLaunchArgs contains a duplicate entry: %q", arg)
		}

		seen[arg] = struct{}{}
	}
}
