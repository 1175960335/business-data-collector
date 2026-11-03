package resume_test

import (
	"bytes"
	"context"
	"encoding/csv"
	"errors"
	"strings"
	"testing"

	"github.com/gosom/google-maps-scraper/gmaps"
	"github.com/gosom/google-maps-scraper/runner/resume"
	"github.com/gosom/scrapemate"
	"github.com/stretchr/testify/require"
)

func TestCSVAppendWriterWritesHeaderForNewFile(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	ids := resume.NewIdentitySet()
	writer := resume.NewCSVAppendWriter(csv.NewWriter(&out), true, ids, nil, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", Title: "A", Link: "https://maps/place/a", PlaceID: "place-a"}}

	close(in)

	require.NoError(t, writer.Run(context.Background(), in))

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	require.Equal(t, "input_id,link,title,category,address,open_hours,popular_times,website,phone,plus_code,review_count,review_rating,reviews_per_rating,latitude,longitude,cid,status,descriptions,reviews_link,thumbnail,timezone,price_range,data_id,street_view_url,place_id,images,reservations,order_online,menu,owner,complete_address,credit_cards_accepted,about,user_reviews,user_reviews_extended,emails", lines[0])
	require.Len(t, lines, 2)
	require.True(t, ids.Has("place-a"))
}

func TestCSVAppendWriterReturnsHeaderWriteError(t *testing.T) {
	t.Parallel()

	ids := resume.NewIdentitySet()
	writer := resume.NewCSVAppendWriter(csv.NewWriter(failingWriter{}), true, ids, nil, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", Title: "A", Link: "https://maps/place/a", PlaceID: "place-a"}}

	close(in)

	require.ErrorContains(t, writer.Run(context.Background(), in), "write failed")
	require.False(t, ids.Has("place-a"))
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) {
	return 0, errors.New("write failed")
}

// TestCSVAppendWriterDoesNotAcknowledgeFailedRowWrite covers the CSV half of the
// write-failure requirement. The header test above only exercises the header;
// this one fails the row write, with the header already suppressed, so the
// failure happens on the data itself.
//
// The property that matters is that a failed write must not acknowledge the
// result. Acknowledging is what marks an input complete in the resume sidecar,
// so acknowledging a write that did not reach disk would make the next run skip
// an input whose data was lost.
func TestCSVAppendWriterDoesNotAcknowledgeFailedRowWrite(t *testing.T) {
	t.Parallel()

	tracker := &recordingResultTracker{}
	writer := resume.NewCSVAppendWriter(csv.NewWriter(failingWriter{}), false, resume.NewIdentitySet(), tracker, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", PlaceID: "place-a"}}

	close(in)

	require.Error(t, writer.Run(context.Background(), in))
	require.Empty(t, tracker.inputIDs, "a failed CSV write must not mark the input complete")
}

// TestCSVAppendWriterDoesNotAcknowledgeWhenFlushFails is the same guarantee for
// the case that actually happens with encoding/csv: Write buffers and reports
// success, and the real I/O error only surfaces at Flush. The acknowledgement
// must still be withheld.
func TestCSVAppendWriterDoesNotAcknowledgeWhenFlushFails(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	tracker := &recordingResultTracker{}
	writer := resume.NewCSVAppendWriter(csv.NewWriter(failingWriter{}), false, resume.NewIdentitySet(), tracker, nil)
	in := make(chan scrapemate.Result, 1)

	// A single row: encoding/csv buffers it, so the failing writer is only hit
	// when the writer flushes.
	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", PlaceID: "place-a"}}

	close(in)

	require.Error(t, writer.Run(context.Background(), in))
	require.Empty(t, out.String())
	require.Empty(t, tracker.inputIDs, "a failed flush must not mark the input complete")
}

func TestCSVAppendWriterSkipsHeaderForExistingFile(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	ids := resume.NewIdentitySet()
	writer := resume.NewCSVAppendWriter(csv.NewWriter(&out), false, ids, nil, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", Title: "A", Link: "https://maps/place/a", PlaceID: "place-a"}}

	close(in)

	require.NoError(t, writer.Run(context.Background(), in))

	require.NotContains(t, out.String(), "input_id,link,title")
	require.True(t, ids.Has("place-a"))
}

func TestJSONLAppendWriterWritesOneObjectPerLine(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	ids := resume.NewIdentitySet()
	writer := resume.NewJSONLAppendWriter(&out, ids, nil, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: []*gmaps.Entry{
		{ID: "q1", Title: "A", Link: "https://maps/place/a", PlaceID: "place-a"},
		{ID: "q1", Title: "B", Link: "https://maps/place/b", Cid: "cid-b"},
	}}

	close(in)

	require.NoError(t, writer.Run(context.Background(), in))

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	require.Len(t, lines, 2)
	require.JSONEq(t, `{"input_id":"q1","link":"https://maps/place/a","title":"A","place_id":"place-a","longitude":0,"categories":null,"open_hours":null,"popular_times":null,"review_count":0,"review_rating":0,"reviews_per_rating":null,"latitude":0,"longtitude":0,"images":null,"reservations":null,"order_online":null,"credit_cards_accepted":null,"about":null,"user_reviews":null,"user_reviews_extended":null,"emails":null,"cid":"","category":"","address":"","web_site":"","phone":"","plus_code":"","status":"","description":"","reviews_link":"","thumbnail":"","timezone":"","price_range":"","data_id":"","street_view_url":"","owner":{"id":"","name":"","link":""},"complete_address":{"borough":"","street":"","city":"","postal_code":"","state":"","country":""},"menu":{"link":"","source":""}}`, lines[0])
	require.True(t, ids.Has("place-a"))
	require.True(t, ids.Has("cid-b"))
}

func TestJSONLAppendWriterSkipsExistingIdentity(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	ids := resume.NewIdentitySet()
	ids.Add("place-a")
	writer := resume.NewJSONLAppendWriter(&out, ids, nil, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: []*gmaps.Entry{
		{ID: "q1", Title: "A", Link: "https://maps/place/a", PlaceID: "place-a"},
		{ID: "q1", Title: "B", Link: "https://maps/place/b", Cid: "cid-b"},
	}}

	close(in)

	require.NoError(t, writer.Run(context.Background(), in))

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	require.Len(t, lines, 1)
	require.Contains(t, lines[0], `"cid":"cid-b"`)
	require.True(t, ids.Has("cid-b"))
}

type recordingResultTracker struct {
	inputIDs []string
}

func (t *recordingResultTracker) ResultPersisted(inputID string, syncOutput func() error) error {
	if syncOutput != nil {
		if err := syncOutput(); err != nil {
			return err
		}
	}

	t.inputIDs = append(t.inputIDs, inputID)

	return nil
}

func TestJSONLAppendWriterAcknowledgesResultAfterWrite(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	tracker := &recordingResultTracker{}
	writer := resume.NewJSONLAppendWriter(&out, resume.NewIdentitySet(), tracker, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", PlaceID: "place-a"}}

	close(in)

	require.NoError(t, writer.Run(context.Background(), in))
	require.Equal(t, []string{"q1"}, tracker.inputIDs)
}

func TestJSONLAppendWriterDoesNotAcknowledgeFailedWrite(t *testing.T) {
	t.Parallel()

	tracker := &recordingResultTracker{}
	writer := resume.NewJSONLAppendWriter(failingWriter{}, resume.NewIdentitySet(), tracker, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", PlaceID: "place-a"}}

	close(in)

	require.Error(t, writer.Run(context.Background(), in))
	require.Empty(t, tracker.inputIDs)
}

func TestJSONLAppendWriterChecksAllExistingIdentities(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	ids := resume.NewIdentitySet()
	ids.Add("cid-a")

	tracker := &recordingResultTracker{}
	writer := resume.NewJSONLAppendWriter(&out, ids, tracker, nil)
	in := make(chan scrapemate.Result, 1)

	in <- scrapemate.Result{Data: &gmaps.Entry{ID: "q1", PlaceID: "place-a", Cid: "cid-a"}}

	close(in)

	require.NoError(t, writer.Run(context.Background(), in))
	require.Empty(t, out.String())
	require.Equal(t, []string{"q1"}, tracker.inputIDs)
}
