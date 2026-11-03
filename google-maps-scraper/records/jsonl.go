package records

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
)

// Stats summarises a normalisation pass. It is returned even when individual
// lines were malformed, so a caller can report "N records, M unusable lines"
// instead of discarding the whole file.
type Stats struct {
	// Lines is the number of non-empty lines read.
	Lines int

	// Records is the number of records written.
	Records int

	// Malformed counts lines that were not valid JSON, or were a JSON value
	// that cannot become a record (a bare number or string, for example).
	Malformed int

	// MalformedLines lists the 1-based line numbers that were skipped, capped
	// at maxReportedMalformed so one bad file cannot produce unbounded output.
	MalformedLines []int

	// SkippedElements counts array elements that were not JSON objects.
	SkippedElements int

	// WithUnknownFields counts records carrying at least one unrecognised key.
	WithUnknownFields int

	// WithMissingFields counts records missing at least one known key.
	WithMissingFields int
}

// maxReportedMalformed caps the recorded line numbers. The count in Malformed
// stays exact.
const maxReportedMalformed = 20

// NormalizeJSONL reads JSONL in which each line is either a single object
// (normal mode) or an array of objects (fast mode), and writes one Record per
// line of output.
//
// Malformed lines are skipped and counted rather than aborting the pass: a
// single corrupt tail from an interrupted run must not make the records that
// were already written unreadable.
func NormalizeJSONL(r io.Reader, w io.Writer, ctx Context) (Stats, error) {
	stats := Stats{}

	scanner := bufio.NewScanner(r)
	// Records can be large; the default 64 KiB token limit truncates them
	// silently, which would look like corruption.
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)

	enc := json.NewEncoder(w)
	lineNo := 0

	for scanner.Scan() {
		lineNo++

		line := scanner.Bytes()
		if len(trimSpace(line)) == 0 {
			continue
		}

		stats.Lines++

		var decoded any
		if err := json.Unmarshal(line, &decoded); err != nil {
			stats.Malformed++
			stats.recordMalformedLine(lineNo)

			continue
		}

		res, err := NormalizeValue(decoded, ctx)
		if err != nil {
			stats.Malformed++
			stats.recordMalformedLine(lineNo)

			continue
		}

		stats.SkippedElements += res.Skipped

		for i := range res.Records {
			if len(res.Records[i].Unknown) > 0 {
				stats.WithUnknownFields++
			}

			if len(res.Records[i].Missing) > 0 {
				stats.WithMissingFields++
			}

			if err := enc.Encode(res.Records[i]); err != nil {
				return stats, fmt.Errorf("records: write record from line %d: %w", lineNo, err)
			}

			stats.Records++
		}
	}

	if err := scanner.Err(); err != nil {
		return stats, fmt.Errorf("records: read input: %w", err)
	}

	return stats, nil
}

func (s *Stats) recordMalformedLine(line int) {
	if len(s.MalformedLines) < maxReportedMalformed {
		s.MalformedLines = append(s.MalformedLines, line)
	}
}

// trimSpace trims ASCII whitespace. JSONL framing only ever uses ASCII
// whitespace, and avoiding unicode tables keeps this on the hot path cheap.
func trimSpace(b []byte) []byte {
	start := 0
	for start < len(b) && isSpace(b[start]) {
		start++
	}

	end := len(b)
	for end > start && isSpace(b[end-1]) {
		end--
	}

	return b[start:end]
}

func isSpace(c byte) bool {
	return c == ' ' || c == '\t' || c == '\r' || c == '\n'
}
