//nolint:testpackage // exercises unexported view helpers and the real mux
package web

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
	"time"
)

// stubJobRepo serves a fixed job so the API handlers can be driven end to end.
type stubJobRepo struct {
	job Job
}

func (r *stubJobRepo) Get(context.Context, string) (Job, error) { return r.job, nil }
func (r *stubJobRepo) Create(_ context.Context, job *Job) error { r.job = *job; return nil }
func (r *stubJobRepo) Delete(context.Context, string) error     { return nil }
func (r *stubJobRepo) Update(_ context.Context, job *Job) error { r.job = *job; return nil }

func (r *stubJobRepo) Select(_ context.Context, _ SelectParams) ([]Job, error) {
	return []Job{r.job}, nil
}

const stubJobID = "6f0c1af8-3c4e-4742-84bb-590938ae8930"

func populatedJob() Job {
	return Job{
		ID:          stubJobID,
		Name:        "Coffee shops Ilion",
		Date:        time.Date(2026, 10, 11, 9, 23, 11, 0, time.UTC),
		Status:      StatusPartial,
		Reason:      "time budget of 180s reached; 12 record(s) written so far were kept",
		ResultCount: 12,
		Data: JobData{
			Keywords:     []string{"coffee in ilion"},
			Lang:         "en",
			Zoom:         15,
			Lat:          "52.5",
			Lon:          "13.4",
			FastMode:     true,
			Radius:       1000,
			Depth:        1,
			Email:        true,
			ExtraReviews: true,
			MaxTime:      3 * time.Minute,
			Proxies:      []string{"http://127.0.0.1:8080"},
		},
	}
}

func decodeObject(t *testing.T, data []byte) map[string]json.RawMessage {
	t.Helper()

	var out map[string]json.RawMessage
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("decode object: %v\n%s", err, data)
	}

	return out
}

func sortedKeysOf(m map[string]json.RawMessage) []string {
	out := make([]string, 0, len(m))

	for k := range m {
		out = append(out, k)
	}

	sort.Strings(out)

	return out
}

// TestJobAndJobViewAgreeOnKeys guards the duplication between the stored struct
// and the documented view: if a field is added to one and not the other, the
// published contract silently stops describing the response.
func TestJobAndJobViewAgreeOnKeys(t *testing.T) {
	t.Parallel()

	job := populatedJob()

	rawJob, err := json.Marshal(job)
	if err != nil {
		t.Fatalf("marshal Job: %v", err)
	}

	rawView, err := json.Marshal(NewJobView(job))
	if err != nil {
		t.Fatalf("marshal JobView: %v", err)
	}

	jobKeys := sortedKeysOf(decodeObject(t, rawJob))
	viewKeys := sortedKeysOf(decodeObject(t, rawView))

	if strings.Join(jobKeys, ",") != strings.Join(viewKeys, ",") {
		t.Fatalf("Job keys %v != JobView keys %v", jobKeys, viewKeys)
	}

	// The nested data object is the one place they deliberately differ.
	dataKeys := sortedKeysOf(decodeObject(t, decodeObject(t, rawJob)["data"]))
	viewDataKeys := sortedKeysOf(decodeObject(t, decodeObject(t, rawView)["data"]))

	diff := difference(viewDataKeys, dataKeys)
	if len(diff) != 1 || diff[0] != "max_time_seconds" {
		t.Fatalf("JobDataView adds %v, want exactly [max_time_seconds]", diff)
	}

	if extra := difference(dataKeys, viewDataKeys); len(extra) != 0 {
		t.Fatalf("JobDataView is missing %v", extra)
	}
}

// TestCanonicalJobJSONUsesDocumentedKeys is the regression for the casing
// mismatch: the API used to emit "ID"/"Name"/"Date"/"Status"/"Data" while the
// OpenAPI document described lower-case keys.
func TestCanonicalJobJSONUsesDocumentedKeys(t *testing.T) {
	t.Parallel()

	raw, err := json.Marshal(NewJobView(populatedJob()))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	got := strings.Join(sortedKeysOf(decodeObject(t, raw)), ",")
	want := "data,date,id,name,reason,result_count,status"

	if got != want {
		t.Fatalf("top-level keys = %q, want %q", got, want)
	}

	for _, legacy := range []string{`"ID":`, `"Name":`, `"Date":`, `"Status":`, `"Data":`} {
		if strings.Contains(string(raw), legacy) {
			t.Fatalf("canonical output still contains the legacy key %s", legacy)
		}
	}
}

// TestMaxTimeInputSecondsOutputNanoseconds documents the unit asymmetry that
// A3 was asked to make explicit: the API accepts seconds and returns
// nanoseconds, and the view adds a seconds field so a client never has to
// remember which is which.
func TestMaxTimeInputSecondsOutputNanoseconds(t *testing.T) {
	t.Parallel()

	view := NewJobView(populatedJob())

	if view.Data.MaxTime != int64(180*time.Second) {
		t.Fatalf("max_time = %d, want %d (nanoseconds)", view.Data.MaxTime, int64(180*time.Second))
	}

	if view.Data.MaxTimeSeconds != 180 {
		t.Fatalf("max_time_seconds = %d, want 180", view.Data.MaxTimeSeconds)
	}
}

// TestLegacyJobViewReproducesPreFixShape checks the adapter that keeps existing
// clients working.
func TestLegacyJobViewReproducesPreFixShape(t *testing.T) {
	t.Parallel()

	job := populatedJob()

	raw, err := json.Marshal(NewLegacyJobViews([]Job{job})[0])
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	got := strings.Join(sortedKeysOf(decodeObject(t, raw)), ",")
	want := "Data,Date,ID,Name,Status,reason,result_count"

	if got != want {
		t.Fatalf("legacy keys = %q, want %q", got, want)
	}

	data := decodeObject(t, decodeObject(t, raw)["Data"])
	if _, present := data["max_time_seconds"]; present {
		t.Fatal("legacy data must not gain max_time_seconds")
	}

	// The legacy body must be byte-identical to marshalling JobData directly,
	// which is what the old handler did.
	direct, err := json.Marshal(job.Data)
	if err != nil {
		t.Fatalf("marshal JobData: %v", err)
	}

	legacyData, err := json.Marshal(data)
	if err != nil {
		t.Fatalf("re-marshal legacy data: %v", err)
	}

	if !sameJSON(t, direct, legacyData) {
		t.Fatalf("legacy data changed:\n direct: %s\n legacy: %s", direct, legacyData)
	}
}

// TestLegacyFormatSelection covers both ways a client can ask for the old shape.
func TestLegacyFormatSelection(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name   string
		target string
		accept string
		want   bool
	}{
		{name: "default", target: "/api/v1/jobs", want: false},
		{name: "query param", target: "/api/v1/jobs?format=legacy", want: true},
		{name: "query param case insensitive", target: "/api/v1/jobs?format=LEGACY", want: true},
		{name: "unrelated format", target: "/api/v1/jobs?format=json", want: false},
		{name: "accept header", target: "/api/v1/jobs", accept: "application/vnd.gms.legacy+json", want: true},
		{name: "accept header among others", target: "/api/v1/jobs",
			accept: "application/json, application/vnd.gms.legacy+json", want: true},
		{name: "plain accept", target: "/api/v1/jobs", accept: "application/json", want: false},
	}

	for _, tc := range cases {
		req := httptest.NewRequest(http.MethodGet, tc.target, http.NoBody)
		if tc.accept != "" {
			req.Header.Set("Accept", tc.accept)
		}

		if got := wantsLegacyJobFormat(req); got != tc.want {
			t.Fatalf("%s: wantsLegacyJobFormat = %v, want %v", tc.name, got, tc.want)
		}
	}

	if wantsLegacyJobFormat(nil) {
		t.Fatal("a nil request must not select the legacy format")
	}
}

// TestApplyMaxTimeSeconds covers the explicit alias, including the conflicting
// case where a client sends two different budgets.
func TestApplyMaxTimeSeconds(t *testing.T) {
	t.Parallel()

	equal := int64(600)
	other := int64(900)
	negative := int64(-1)
	only := int64(3600)

	cases := []struct {
		name       string
		maxTime    time.Duration
		alias      *int64
		wantErr    string
		wantResult time.Duration
	}{
		{name: "no alias leaves max_time alone", maxTime: 600, wantResult: 600},
		{name: "alias agrees", maxTime: 600, alias: &equal, wantResult: 600},
		{name: "alias disagrees", maxTime: 600, alias: &other, wantErr: "disagree"},
		{name: "alias is negative", alias: &negative, wantErr: "must not be negative"},
		{name: "alias is the only source", alias: &only, wantResult: 3600},
	}

	for _, tc := range cases {
		data := JobData{MaxTime: tc.maxTime}

		err := applyMaxTimeSeconds(&data, tc.alias)

		if tc.wantErr != "" {
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("%s: err = %v, want containing %q", tc.name, err, tc.wantErr)
			}

			continue
		}

		if err != nil {
			t.Fatalf("%s: unexpected error %v", tc.name, err)
		}

		if data.MaxTime != tc.wantResult {
			t.Fatalf("%s: max_time = %d, want %d", tc.name, data.MaxTime, tc.wantResult)
		}
	}
}

// TestValidateBudgetSeconds covers the bound applied before seconds are
// converted to a duration.
func TestValidateBudgetSeconds(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name    string
		seconds time.Duration
		wantErr string
	}{
		{name: "zero is allowed here", seconds: 0},
		{name: "typical", seconds: 3600},
		{name: "exactly the cap", seconds: time.Duration(maxBudgetSeconds)},
		{name: "over the cap", seconds: time.Duration(maxBudgetSeconds + 1), wantErr: "at most"},
		{name: "negative", seconds: -1, wantErr: "must not be negative"},
	}

	for _, tc := range cases {
		err := validateBudgetSeconds(tc.seconds)

		if tc.wantErr == "" {
			if err != nil {
				t.Fatalf("%s: unexpected error %v", tc.name, err)
			}

			continue
		}

		if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
			t.Fatalf("%s: err = %v, want containing %q", tc.name, err, tc.wantErr)
		}
	}
}

// serve drives a request through the real handler chain, so path values and
// middleware behave as they do in production.
func serve(t *testing.T, srv *Server, req *http.Request) *httptest.ResponseRecorder {
	t.Helper()

	rec := httptest.NewRecorder()
	srv.srv.Handler.ServeHTTP(rec, req)

	return rec
}

// TestAPICreateAndReadJobRoundTrip is the end-to-end contract check: the body
// in the OpenAPI document is accepted, and the response uses the documented
// keys.
func TestAPICreateAndReadJobRoundTrip(t *testing.T) {
	t.Parallel()

	repo := &stubJobRepo{}
	srv := newTestServerWithRepo(t, repo)

	body := `{
		"name": "Coffee shops Ilion",
		"keywords": ["coffee in ilion"],
		"lang": "en",
		"zoom": 15,
		"depth": 1,
		"max_time": 3600
	}`

	createReq := httptest.NewRequest(http.MethodPost, "/api/v1/jobs", strings.NewReader(body))
	createReq.Header.Set("Content-Type", "application/json")

	rec := serve(t, srv, createReq)
	if rec.Code != http.StatusCreated {
		t.Fatalf("create status = %d, want 201: %s", rec.Code, rec.Body.String())
	}

	var created apiScrapeResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &created); err != nil {
		t.Fatalf("decode create response: %v", err)
	}

	if created.ID == "" {
		t.Fatal("create response did not include an id")
	}

	if repo.job.Name != "Coffee shops Ilion" {
		t.Fatalf("stored name = %q, want the documented lower-case key to be read", repo.job.Name)
	}

	// The stored budget must be a real duration: 3600 seconds.
	if repo.job.Data.MaxTime != time.Hour {
		t.Fatalf("stored max_time = %v, want 1h", repo.job.Data.MaxTime)
	}

	readReq := httptest.NewRequest(http.MethodGet, "/api/v1/jobs/"+created.ID, http.NoBody)

	read := serve(t, srv, readReq)
	if read.Code != http.StatusOK {
		t.Fatalf("read status = %d, want 200: %s", read.Code, read.Body.String())
	}

	keys := strings.Join(sortedKeysOf(decodeObject(t, read.Body.Bytes())), ",")
	if keys != "data,date,id,name,result_count,status" {
		t.Fatalf("read keys = %q", keys)
	}

	var got JobView
	if err := json.Unmarshal(read.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode read response: %v", err)
	}

	if got.Data.MaxTime != int64(time.Hour) {
		t.Fatalf("returned max_time = %d, want %d nanoseconds", got.Data.MaxTime, int64(time.Hour))
	}

	if got.Data.MaxTimeSeconds != 3600 {
		t.Fatalf("returned max_time_seconds = %d, want 3600", got.Data.MaxTimeSeconds)
	}
}

// TestAPIListHonoursLegacyFormat covers the list endpoint's adapter path.
func TestAPIListHonoursLegacyFormat(t *testing.T) {
	t.Parallel()

	srv := newTestServerWithRepo(t, &stubJobRepo{job: populatedJob()})

	canonical := serve(t, srv, httptest.NewRequest(http.MethodGet, "/api/v1/jobs", http.NoBody))
	if canonical.Code != http.StatusOK {
		t.Fatalf("status = %d", canonical.Code)
	}

	var canonicalJobs []map[string]json.RawMessage
	if err := json.Unmarshal(canonical.Body.Bytes(), &canonicalJobs); err != nil {
		t.Fatalf("decode canonical list: %v", err)
	}

	if _, ok := canonicalJobs[0]["id"]; !ok {
		t.Fatalf("canonical list is missing the lower-case id key: %s", canonical.Body.String())
	}

	legacy := serve(t, srv, httptest.NewRequest(http.MethodGet, "/api/v1/jobs?format=legacy", http.NoBody))

	var legacyJobs []map[string]json.RawMessage
	if err := json.Unmarshal(legacy.Body.Bytes(), &legacyJobs); err != nil {
		t.Fatalf("decode legacy list: %v", err)
	}

	if _, ok := legacyJobs[0]["ID"]; !ok {
		t.Fatalf("legacy list is missing the upper-case ID key: %s", legacy.Body.String())
	}

	if _, ok := legacyJobs[0]["id"]; ok {
		t.Fatal("legacy list must not also emit the lower-case id key")
	}
}

// TestAPICreateRejectsConflictingMaxTime covers the alias guard over HTTP.
func TestAPICreateRejectsConflictingMaxTime(t *testing.T) {
	t.Parallel()

	srv := newTestServerWithRepo(t, &stubJobRepo{})

	body := `{"name":"x","keywords":["k"],"lang":"en","depth":1,"max_time":600,"max_time_seconds":900}`

	rec := serve(t, srv, httptest.NewRequest(http.MethodPost, "/api/v1/jobs", strings.NewReader(body)))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422: %s", rec.Code, rec.Body.String())
	}

	if !strings.Contains(rec.Body.String(), "disagree") {
		t.Fatalf("body does not explain the conflict: %s", rec.Body.String())
	}
}

// TestAPICreateAcceptsMaxTimeSecondsAlone covers a new client that only knows
// the unambiguous field.
func TestAPICreateAcceptsMaxTimeSecondsAlone(t *testing.T) {
	t.Parallel()

	repo := &stubJobRepo{}
	srv := newTestServerWithRepo(t, repo)

	body := `{"name":"x","keywords":["k"],"lang":"en","depth":1,"max_time_seconds":3600}`

	rec := serve(t, srv, httptest.NewRequest(http.MethodPost, "/api/v1/jobs", strings.NewReader(body)))
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201: %s", rec.Code, rec.Body.String())
	}

	if repo.job.Data.MaxTime != time.Hour {
		t.Fatalf("stored max_time = %v, want 1h", repo.job.Data.MaxTime)
	}
}

// TestAPICreateRejectsOverBudget covers the pre-conversion bound.
func TestAPICreateRejectsOverBudget(t *testing.T) {
	t.Parallel()

	srv := newTestServerWithRepo(t, &stubJobRepo{})

	body := `{"name":"x","keywords":["k"],"lang":"en","depth":1,"max_time":9999999999}`

	rec := serve(t, srv, httptest.NewRequest(http.MethodPost, "/api/v1/jobs", strings.NewReader(body)))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422: %s", rec.Code, rec.Body.String())
	}
}

// TestAPIReportsTerminalStatusReasonAndResultCount is the A2 acceptance check
// at the HTTP layer: a non-ok terminal state must reach the client with the
// explanation and the record count, not just as a bare status string.
func TestAPIReportsTerminalStatusReasonAndResultCount(t *testing.T) {
	t.Parallel()

	for _, status := range []string{StatusPartial, StatusFailed, StatusCancelled, StatusInterrupted} {
		job := populatedJob()
		job.Status = status

		srv := newTestServerWithRepo(t, &stubJobRepo{job: job})

		rec := serve(t, srv, httptest.NewRequest(http.MethodGet, "/api/v1/jobs/"+stubJobID, http.NoBody))
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status = %d", status, rec.Code)
		}

		var got JobView
		if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
			t.Fatalf("%s: decode: %v", status, err)
		}

		if got.Status != status {
			t.Fatalf("status = %q, want %q", got.Status, status)
		}

		if got.Reason == "" {
			t.Fatalf("%s: reason was not reported, so the client cannot tell why", status)
		}

		if got.ResultCount != 12 {
			t.Fatalf("%s: result_count = %d, want 12", status, got.ResultCount)
		}

		if !IsTerminal(got.Status) {
			t.Fatalf("%s: reported status is not terminal", status)
		}
	}
}

func difference(a, b []string) []string {
	set := make(map[string]struct{}, len(b))
	for _, s := range b {
		set[s] = struct{}{}
	}

	var out []string

	for _, s := range a {
		if _, ok := set[s]; !ok {
			out = append(out, s)
		}
	}

	return out
}

func sameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()

	var va, vb any
	if err := json.Unmarshal(a, &va); err != nil {
		t.Fatalf("unmarshal a: %v", err)
	}

	if err := json.Unmarshal(b, &vb); err != nil {
		t.Fatalf("unmarshal b: %v", err)
	}

	ra, err := json.Marshal(va)
	if err != nil {
		t.Fatalf("re-marshal a: %v", err)
	}

	rb, err := json.Marshal(vb)
	if err != nil {
		t.Fatalf("re-marshal b: %v", err)
	}

	return string(ra) == string(rb)
}
