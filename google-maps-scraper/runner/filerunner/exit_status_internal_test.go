//nolint:testpackage // tests the unexported exit-code mapping directly
package filerunner

import (
	"testing"
)

// TestExitStatus covers the CLI exit-code decision. scrapemate's Start returns
// nil once the scheduler stops, even when every job failed, so the exit status
// has to come from the job counters. Getting this wrong is how a total failure
// used to exit 0 and look like success.
func TestExitStatus(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name      string
		completed int64
		failed    int64
		wantErr   bool
	}{
		{
			name:      "empty input is a clean run, not a failure",
			completed: 0,
			failed:    0,
			wantErr:   false,
		},
		{
			name:      "every job succeeded",
			completed: 5,
			failed:    0,
			wantErr:   false,
		},
		{
			name:      "every job failed",
			completed: 0,
			failed:    3,
			wantErr:   true,
		},
		{
			name:      "some jobs failed but records exist",
			completed: 2,
			failed:    3,
			wantErr:   false,
		},
	}

	for _, tc := range cases {
		err := exitStatus(tc.completed, tc.failed)

		if tc.wantErr && err == nil {
			t.Fatalf("%s: exitStatus(%d, %d) = nil, want an error",
				tc.name, tc.completed, tc.failed)
		}

		if !tc.wantErr && err != nil {
			t.Fatalf("%s: exitStatus(%d, %d) = %v, want nil",
				tc.name, tc.completed, tc.failed, err)
		}
	}
}

// TestExitStatusMentionsTheFailureCount keeps the message diagnostic: a caller
// reading the CLI output must be able to tell how many jobs failed.
func TestExitStatusMentionsTheFailureCount(t *testing.T) {
	t.Parallel()

	err := exitStatus(0, 7)
	if err == nil {
		t.Fatal("expected an error")
	}

	const want = "7 failed"

	if got := err.Error(); !contains(got, want) {
		t.Fatalf("error %q does not mention %q", got, want)
	}
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}

	return false
}
