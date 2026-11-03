package grid_test

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gosom/google-maps-scraper/grid"
)

func TestParseBoundingBox(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		in      string
		wantErr string
	}{
		{
			name: "valid",
			in:   "40.30,-3.80,40.50,-3.60",
		},
		{
			name:    "invalid min latitude",
			in:      "-91,0,10,10",
			wantErr: "minLat",
		},
		{
			name:    "invalid max longitude",
			in:      "10,0,20,181",
			wantErr: "maxLon",
		},
		{
			name:    "non finite value",
			in:      "NaN,0,10,10",
			wantErr: "must be finite",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			_, err := grid.ParseBoundingBox(tc.in)
			if tc.wantErr == "" && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("expected error containing %q, got nil", tc.wantErr)
				}

				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected error containing %q, got %q", tc.wantErr, err.Error())
				}
			}
		})
	}
}

func TestGenerateCellsAndEstimateCellCountNearPoles(t *testing.T) {
	t.Parallel()

	bbox := grid.BoundingBox{
		MinLat: 89.90,
		MinLon: 0.00,
		MaxLat: 89.95,
		MaxLon: 20.00,
	}

	cellSizeKm := 1.0

	gotCells := grid.GenerateCells(bbox, cellSizeKm)
	if len(gotCells) == 0 {
		t.Fatal("expected cells to be generated near poles")
	}

	for _, c := range gotCells {
		if math.IsNaN(c.Lat) || math.IsInf(c.Lat, 0) || math.IsNaN(c.Lon) || math.IsInf(c.Lon, 0) {
			t.Fatalf("expected finite cell coordinates, got %+v", c)
		}
	}

	gotCount := grid.EstimateCellCount(bbox, cellSizeKm)
	if gotCount != len(gotCells) {
		t.Fatalf("expected EstimateCellCount=%d to match generated cells=%d", gotCount, len(gotCells))
	}
}

// TestEstimateCellCountMatchesGenerateCells pins the property that the number
// printed to the operator before a grid run equals the number of cells that run
// actually creates.
//
// The previous implementation used ceil(span/step) while GenerateCells only
// places a centre while it is strictly below max, so the estimate over-reported
// by up to 56%. The near-pole case above was the one shape where the two
// happened to agree, which is why the defect survived its only test.
func TestEstimateCellCountMatchesGenerateCells(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name     string
		bbox     grid.BoundingBox
		cellSize float64
		want     int
	}{
		{
			name:     "berlin mitte 2.2km by 2.0km with 0.5km cells",
			bbox:     grid.BoundingBox{MinLat: 52.515, MinLon: 13.385, MaxLat: 52.535, MaxLon: 13.415},
			cellSize: 0.5,
			want:     16,
		},
		{
			name:     "20km by 20km with 1km cells",
			bbox:     grid.BoundingBox{MinLat: 52.4, MinLon: 13.3, MaxLat: 52.579659, MaxLon: 13.583782},
			cellSize: 1.0,
			want:     380,
		},
		{
			name:     "whole number of kilometres per axis",
			bbox:     grid.BoundingBox{MinLat: 52.4, MinLon: 13.3, MaxLat: 52.5, MaxLon: 13.4},
			cellSize: 1.0,
			want:     77,
		},
		{
			name:     "span narrower than half a cell yields no cells",
			bbox:     grid.BoundingBox{MinLat: 52.5, MinLon: 13.4, MaxLat: 52.5001, MaxLon: 13.4001},
			cellSize: 1.0,
			want:     0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			got := len(grid.GenerateCells(tc.bbox, tc.cellSize))
			estimated := grid.EstimateCellCount(tc.bbox, tc.cellSize)

			if got != tc.want {
				t.Fatalf("GenerateCells produced %d cells, want %d", got, tc.want)
			}

			if estimated != got {
				t.Fatalf("EstimateCellCount reported %d but GenerateCells produced %d", estimated, got)
			}
		})
	}
}

// TestGenerateCellsPlacesCentersInsideBBoxAndOneStepApart checks the geometry
// the grid relies on: every centre is inside the requested box, and centres are
// spaced exactly one cell apart on each axis so the cells tile rather than
// leaving a gap between centres.
func TestGenerateCellsPlacesCentersInsideBBoxAndOneStepApart(t *testing.T) {
	t.Parallel()

	bbox := grid.BoundingBox{MinLat: 52.515, MinLon: 13.385, MaxLat: 52.535, MaxLon: 13.415}
	cellSizeKm := 0.5

	cells := grid.GenerateCells(bbox, cellSizeKm)
	if len(cells) < 2 {
		t.Fatalf("expected a multi-cell grid, got %d cells", len(cells))
	}

	for _, c := range cells {
		if !bbox.Contains(c.Lat, c.Lon) {
			t.Fatalf("cell centre %+v is outside the bounding box %+v", c, bbox)
		}
	}

	// Cells are emitted row-major with longitude varying fastest, so consecutive
	// cells either advance by one longitude step within a row, or start the next
	// row one latitude step up at the row's first longitude.
	latStep := cellSizeKm / 111.32
	lonStep := cellSizeKm / (111.32 * math.Cos((bbox.MinLat+bbox.MaxLat)/2*math.Pi/180))

	lonCells := 0
	for _, c := range cells {
		if math.Abs(c.Lat-cells[0].Lat) > 1e-12 {
			break
		}

		lonCells++
	}

	if lonCells < 2 {
		t.Fatalf("expected more than one column, got %d", lonCells)
	}

	for i := 1; i < len(cells); i++ {
		prev, cur := cells[i-1], cells[i]

		if i%lonCells != 0 {
			if math.Abs(cur.Lat-prev.Lat) > 1e-12 {
				t.Fatalf("cells %d and %d are in the same row but have different latitudes: %+v %+v", i-1, i, prev, cur)
			}

			if diff := math.Abs(cur.Lon - prev.Lon); math.Abs(diff-lonStep) > 1e-9 {
				t.Fatalf("longitude step between %d and %d is %f, want %f", i-1, i, diff, lonStep)
			}

			continue
		}

		if diff := math.Abs(cur.Lat - prev.Lat); math.Abs(diff-latStep) > 1e-9 {
			t.Fatalf("latitude step between rows at cell %d is %f, want %f", i, diff, latStep)
		}

		if math.Abs(cur.Lon-cells[0].Lon) > 1e-12 {
			t.Fatalf("cell %d starts a new row at longitude %f, want the first column %f", i, cur.Lon, cells[0].Lon)
		}
	}
}

// TestGenerateCellsNonPositiveCellSizeFallsBackToOneKilometre pins the
// documented default so an unset or negative -grid-cell cannot silently produce
// a degenerate grid.
func TestGenerateCellsNonPositiveCellSizeFallsBackToOneKilometre(t *testing.T) {
	t.Parallel()

	bbox := grid.BoundingBox{MinLat: 52.4, MinLon: 13.3, MaxLat: 52.6, MaxLon: 13.5}

	for _, size := range []float64{0, -1, -0.5} {
		got := grid.GenerateCells(bbox, size)
		want := grid.GenerateCells(bbox, 1.0)

		if len(got) != len(want) {
			t.Fatalf("cell size %v produced %d cells, want the 1km default of %d", size, len(got), len(want))
		}
	}
}

// TestContainsOnRealGridOutput is a snapshot of a real grid run, used to pin the
// fact that a grid scrape returns a large share of records from outside the
// requested box.
//
// The sample is the 35 records collected on 2026-10-11 by
//   -grid-bbox 52.515,13.385,52.535,13.415 -grid-cell 1.0 -zoom 15
// (4 cells, one query, Berlin). Only title/latitude/longitude were kept.
//
// It exists so that the coverage claim in the report is checkable rather than
// asserted: 14 records fall inside the box and 21 do not. Callers that need a
// strict geographic cut must filter; the scraper keeps the out-of-box records.
func TestContainsOnRealGridOutput(t *testing.T) {
	t.Parallel()

	raw, err := os.ReadFile(filepath.Join("testdata", "grid-results.jsonl"))
	if err != nil {
		t.Fatalf("read grid snapshot: %v", err)
	}

	bbox := grid.BoundingBox{MinLat: 52.515, MinLon: 13.385, MaxLat: 52.535, MaxLon: 13.415}

	var inside, outside int

	lines := 0

	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}

		lines++

		var rec struct {
			Title     string  `json:"title"`
			Latitude  float64 `json:"latitude"`
			Longitude float64 `json:"longitude"`
		}

		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("parse snapshot line %d: %v", lines, err)
		}

		if bbox.Contains(rec.Latitude, rec.Longitude) {
			inside++
		} else {
			outside++
		}
	}

	if lines != 35 {
		t.Fatalf("snapshot has %d records, expected 35", lines)
	}

	if inside != 14 || outside != 21 {
		t.Fatalf("expected 14 inside and 21 outside the box, got %d inside and %d outside", inside, outside)
	}
}

// TestContains covers the predicate used to cut collected records down to the
// requested box. Google Maps returns places outside the requested area, so this
// is what an explicit post-filter relies on.
func TestContains(t *testing.T) {
	t.Parallel()

	bbox := grid.BoundingBox{MinLat: 52.515, MinLon: 13.385, MaxLat: 52.535, MaxLon: 13.415}

	tests := []struct {
		name string
		lat  float64
		lon  float64
		want bool
	}{
		{name: "centre", lat: 52.525, lon: 13.400, want: true},
		{name: "south west corner is included", lat: 52.515, lon: 13.385, want: true},
		{name: "north east corner is included", lat: 52.535, lon: 13.415, want: true},
		{name: "just inside the north edge", lat: 52.5349, lon: 13.400, want: true},
		{name: "north of the box", lat: 52.5351, lon: 13.400, want: false},
		{name: "south of the box", lat: 52.5149, lon: 13.400, want: false},
		{name: "east of the box", lat: 52.525, lon: 13.4151, want: false},
		{name: "west of the box", lat: 52.525, lon: 13.3849, want: false},
		{name: "null island is not inside a berlin box", lat: 0, lon: 0, want: false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			if got := bbox.Contains(tc.lat, tc.lon); got != tc.want {
				t.Fatalf("Contains(%v, %v) = %v, want %v", tc.lat, tc.lon, got, tc.want)
			}
		})
	}
}
