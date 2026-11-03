package gmaps

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/PuerkitoBio/goquery"
	"github.com/gosom/scrapemate"
	"github.com/stretchr/testify/require"
)

// countingExiter is a minimal exiter.Exiter that records how many places the
// email job reported as completed. Only the counter the job touches is
// implemented with real behaviour; the rest are no-ops.
type countingExiter struct {
	placesCompleted int
}

func (e *countingExiter) SetSeedCount(int)                 {}
func (e *countingExiter) SetCancelFunc(context.CancelFunc) {}
func (e *countingExiter) IncrSeedCompleted(int)            {}
func (e *countingExiter) IncrPlacesFound(int)              {}
func (e *countingExiter) IncrPlacesCompleted(n int)        { e.placesCompleted += n }
func (e *countingExiter) Run(context.Context)              {}

// documentFromHTML builds the goquery document that scrapemate would attach to
// a successful response for the given HTML body.
func documentFromHTML(t *testing.T, html string) *goquery.Document {
	t.Helper()

	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	require.NoError(t, err)

	return doc
}

// TestEmailJobExtractsMailtoLinks pins the primary extraction path: an anchor
// whose href is a mailto: URL is the strongest evidence a site publishes, so it
// is used directly without falling back to the body-text regex.
func TestEmailJobExtractsMailtoLinks(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	doc := documentFromHTML(t, `<html><body>
		<a href="mailto:info@example.com">Contact</a>
		<a href="mailto:sales@example.com">Sales</a>
	</body></html>`)

	result, next, err := job.Process(context.Background(), &scrapemate.Response{
		Document: doc,
		Body:     []byte("<html><body><a href=\"mailto:info@example.com\">Contact</a></body></html>"),
	})

	require.NoError(t, err)
	require.Empty(t, next)
	require.Same(t, entry, result)
	require.Equal(t, []string{"info@example.com", "sales@example.com"}, entry.Emails)
}

// TestEmailJobFallsBackToBodyText covers pages that render the address as plain
// text, obfuscate the anchor, or wrap it in a form. The mailto path finds
// nothing there, so the body-wide regex has to take over.
func TestEmailJobFallsBackToBodyText(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	doc := documentFromHTML(t, `<html><body><p>Write to us: kontakt@beispiel.de</p></body></html>`)

	result, next, err := job.Process(context.Background(), &scrapemate.Response{
		Document: doc,
		Body:     []byte(`<html><body><p>Write to us: kontakt@beispiel.de</p></body></html>`),
	})

	require.NoError(t, err)
	require.Empty(t, next)
	require.Same(t, entry, result)
	require.Equal(t, []string{"kontakt@beispiel.de"}, entry.Emails)
}

// TestEmailJobPrefersMailtoOverBodyText documents a real ordering property of
// the extractor: the regex fallback only runs when the mailto path returns
// nothing. A site that publishes one address in a link and a different one in
// body text therefore yields only the linked address. This is recorded as
// observed behaviour, not as an endorsement.
func TestEmailJobPrefersMailtoOverBodyText(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	html := `<html><body>
		<a href="mailto:linked@example.com">Contact</a>
		<p>Unlinked address: plain@example.com</p>
	</body></html>`

	result, _, err := job.Process(context.Background(), &scrapemate.Response{
		Document: documentFromHTML(t, html),
		Body:     []byte(html),
	})

	require.NoError(t, err)
	require.Same(t, entry, result)
	require.Equal(t, []string{"linked@example.com"}, entry.Emails,
		"the body-text regex must not run once the mailto path produced a hit")
}

// TestEmailJobDeduplicatesRepeatedAddresses guards against the same address
// being emitted once per occurrence, which would inflate the CSV emails column.
func TestEmailJobDeduplicatesRepeatedAddresses(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	html := `<html><body>
		<a href="mailto:info@example.com">Contact</a>
		<a href="mailto:info@example.com">Contact again</a>
	</body></html>`

	result, _, err := job.Process(context.Background(), &scrapemate.Response{
		Document: documentFromHTML(t, html),
		Body:     []byte(html),
	})

	require.NoError(t, err)
	require.Same(t, entry, result)
	require.Equal(t, []string{"info@example.com"}, entry.Emails)
}

// TestEmailJobIgnoresInvalidMailtoValues checks that a syntactically invalid
// mailto target is not reported as an email. Invalid values must not become
// evidence just because they appeared in a mailto anchor.
func TestEmailJobIgnoresInvalidMailtoValues(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	doc := documentFromHTML(t, `<html><body>
		<a href="mailto:not-an-address">Bad</a>
		<a href="mailto:@example.com">Bad</a>
		<a href="mailto:good@example.com">Good</a>
	</body></html>`)

	result, _, err := job.Process(context.Background(), &scrapemate.Response{
		Document: doc,
		Body:     []byte(`<html><body><a href="mailto:not-an-address">Bad</a></body></html>`),
	})

	require.NoError(t, err)
	require.Same(t, entry, result)
	require.Equal(t, []string{"good@example.com"}, entry.Emails)
}

// TestEmailJobMailtoWithQueryStringRecoversViaBodyText covers the common
// `mailto:address?subject=...` form. The mailto path cannot parse the query
// suffix, so the result must still be recovered by the body-text fallback
// rather than being silently dropped.
func TestEmailJobMailtoWithQueryStringRecoversViaBodyText(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	html := `<html><body><a href="mailto:vertrieb@beispiel.de?subject=Anfrage">Anfrage</a></body></html>`

	result, _, err := job.Process(context.Background(), &scrapemate.Response{
		Document: documentFromHTML(t, html),
		Body:     []byte(html),
	})

	require.NoError(t, err)
	require.Same(t, entry, result)
	require.Equal(t, []string{"vertrieb@beispiel.de"}, entry.Emails)
}

// TestEmailJobPageWithoutAnyEmail is the negative control for the whole
// capability: a reachable page that publishes no address must produce no
// evidence at all. Filling a placeholder here would be fabrication.
func TestEmailJobPageWithoutAnyEmail(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	html := `<html><body><h1>We have no published address</h1><p>Use the contact form.</p></body></html>`

	result, next, err := job.Process(context.Background(), &scrapemate.Response{
		Document: documentFromHTML(t, html),
		Body:     []byte(html),
	})

	require.NoError(t, err)
	require.Empty(t, next)
	require.Same(t, entry, result)
	require.Empty(t, entry.Emails)
}

// TestEmailJobFetchFailureKeepsEntryUnchanged covers the request-failure path.
// The entry must survive with no email rather than being dropped or given a
// guessed value, because the email job is a decoration on an already-collected
// place.
func TestEmailJobFetchFailureKeepsEntryUnchanged(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	job := NewEmailJob("place-1", entry)

	result, next, err := job.Process(context.Background(), &scrapemate.Response{
		Error: errors.New("dial tcp: connection refused"),
	})

	require.NoError(t, err, "a failed email fetch is not a job error")
	require.Empty(t, next)
	require.Same(t, entry, result)
	require.Empty(t, entry.Emails)
}

// TestEmailJobUnexpectedDocumentTypeIsNotFatal covers a response whose body is
// not an HTML document. The type assertion must fail closed, not panic.
func TestEmailJobUnexpectedDocumentTypeIsNotFatal(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		doc  any
	}{
		{name: "nil document", doc: nil},
		{name: "plain string document", doc: "<html><body>hi</body></html>"},
		{name: "byte document", doc: []byte("hi")},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
			job := NewEmailJob("place-1", entry)

			result, next, err := job.Process(context.Background(), &scrapemate.Response{
				Document: tc.doc,
				Body:     []byte("plain@example.com"),
			})

			require.NoError(t, err)
			require.Empty(t, next)
			require.Same(t, entry, result)
			require.Empty(t, entry.Emails,
				"a non-HTML document must not be mined by the regex fallback")
		})
	}
}

// TestEmailJobProcessOnFetchErrorIsHandled confirms the job opts into being
// invoked with a failed response instead of being retried or dropped by the
// scheduler.
func TestEmailJobProcessOnFetchErrorIsHandled(t *testing.T) {
	t.Parallel()

	job := NewEmailJob("place-1", &Entry{ID: "input-1", WebSite: "https://example.com"})

	require.True(t, job.ProcessOnFetchError())
}

// TestEmailJobDoesNotCountCompletionWhenWriterManagesIt guards the interaction
// between the email job and the resume progress tracker: when the writer is
// responsible for acknowledging a persisted result, the job must not also
// increment the completed counter, or inputs would be marked complete before
// their result was durable.
func TestEmailJobDoesNotCountCompletionWhenWriterManagesIt(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	monitor := &countingExiter{}
	job := NewEmailJob("place-1", entry,
		WithEmailJobExitMonitor(monitor),
		WithEmailJobWriterManagedCompletion(),
	)

	html := `<html><body><a href="mailto:info@example.com">Contact</a></body></html>`

	_, _, err := job.Process(context.Background(), &scrapemate.Response{
		Document: documentFromHTML(t, html),
		Body:     []byte(html),
	})

	require.NoError(t, err)
	require.Equal(t, 0, monitor.placesCompleted,
		"writer-managed completion must suppress the job's own counter")
}

// TestEmailJobCountsCompletionWithoutWriterManagement is the positive control
// for the previous test: without the writer-managed flag the counter must move,
// otherwise the suppression above could pass by accident.
func TestEmailJobCountsCompletionWithoutWriterManagement(t *testing.T) {
	t.Parallel()

	entry := &Entry{ID: "input-1", WebSite: "https://example.com"}
	monitor := &countingExiter{}
	job := NewEmailJob("place-1", entry, WithEmailJobExitMonitor(monitor))

	html := `<html><body><a href="mailto:info@example.com">Contact</a></body></html>`

	_, _, err := job.Process(context.Background(), &scrapemate.Response{
		Document: documentFromHTML(t, html),
		Body:     []byte(html),
	})

	require.NoError(t, err)
	require.Equal(t, 1, monitor.placesCompleted)
}

// TestEmailJobNormalizesGoogleRedirectURLs covers the website values Google
// Maps returns for outbound links. A redirect wrapper or a site-relative path
// must be resolved before the fetch, otherwise the email job requests a Google
// page and extracts nothing.
func TestEmailJobNormalizesGoogleRedirectURLs(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		site string
		want string
	}{
		{
			name: "absolute url is untouched",
			site: "https://example.com/contact",
			want: "https://example.com/contact",
		},
		{
			name: "google redirect wrapper is unwrapped",
			site: "/url?q=https://example.com/&opi=89978449",
			want: "https://example.com/",
		},
		{
			name: "site relative path becomes a google url",
			site: "/maps/place/foo",
			want: "https://www.google.com/maps/place/foo",
		},
		{
			name: "empty stays empty",
			site: "",
			want: "",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			job := NewEmailJob("place-1", &Entry{ID: "input-1", WebSite: tc.site})
			require.Equal(t, tc.want, job.URL)
		})
	}
}

// TestRegexEmailExtractorAcceptsInternationalAddresses guards the non-ASCII and
// multi-label cases that a naive pattern would miss.
func TestRegexEmailExtractorAcceptsInternationalAddresses(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name string
		body string
		want []string
	}{
		{
			name: "plus addressing",
			body: "reach sales+de@example.com for orders",
			want: []string{"sales+de@example.com"},
		},
		{
			name: "multi label domain",
			body: "mail info@mail.example.co.uk today",
			want: []string{"info@mail.example.co.uk"},
		},
		{
			name: "uppercase is preserved as written",
			body: "write to Info@Example.COM",
			want: []string{"Info@Example.COM"},
		},
		{
			name: "two addresses in one paragraph",
			body: "a@example.com and b@example.org",
			want: []string{"a@example.com", "b@example.org"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			require.Equal(t, tc.want, regexEmailExtractor([]byte(tc.body)))
		})
	}
}

// TestGetValidEmailRejectsMalformedInput is the negative control for the mailto
// parser.
func TestGetValidEmailRejectsMalformedInput(t *testing.T) {
	t.Parallel()

	valid := []string{"info@example.com", " sales@example.com "}

	for _, in := range valid {
		got, err := getValidEmail(in)
		require.NoError(t, err, "input %q", in)
		require.Equal(t, strings.TrimSpace(in), got)
	}

	invalid := []string{"", "plain-text", "@example.com", "info@", "info@@example.com"}

	for _, in := range invalid {
		_, err := getValidEmail(in)
		require.Error(t, err, "input %q should not parse as an email", in)
	}
}
