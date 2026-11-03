//nolint:testpackage // needs the unexported view types and the embedded spec file
package web

import (
	"encoding/json"
	"os"
	"sort"
	"strings"
	"testing"
)

// The OpenAPI document is served to clients and is the published contract, but
// nothing verified that it described the actual response. These tests read the
// real spec file and compare it with the real marshalled response, so a field
// added to one and not the other fails the build instead of shipping a
// document that lies.

const specPath = "static/spec/spec.yaml"

// specSchemaProperties extracts the property names of one schema from the
// OpenAPI document.
//
// It deliberately does not use a YAML library: the module only has YAML as an
// indirect dependency and promoting it just for a test is not worth it. The
// parser is anchored on exact indentation and fails loudly rather than
// returning an empty set, so reformatting the document produces a visible
// failure instead of a test that silently passes.
func specSchemaProperties(t *testing.T, schema string) []string {
	t.Helper()

	data, err := os.ReadFile(specPath)
	if err != nil {
		t.Fatalf("read %s: %v", specPath, err)
	}

	// The document is checked out with CRLF line endings on Windows, so
	// normalise before matching anything.
	text := strings.ReplaceAll(string(data), "\r\n", "\n")

	lines := strings.Split(text, "\n")

	const (
		schemaIndent     = 4
		propertiesIndent = 6
		keyIndent        = 8
	)

	schemaHeader := strings.Repeat(" ", schemaIndent) + schema + ":"

	start := -1

	for i, line := range lines {
		if line == schemaHeader {
			start = i

			break
		}
	}

	if start < 0 {
		t.Fatalf("%s: schema %q not found at indent %d; the document was restructured "+
			"and this test can no longer read it", specPath, schema, schemaIndent)
	}

	properties := []string{}

	for i := start + 1; i < len(lines); i++ {
		line := lines[i]

		if strings.TrimSpace(line) == "" || strings.HasPrefix(strings.TrimSpace(line), "#") {
			continue
		}

		indent := len(line) - len(strings.TrimLeft(line, " "))

		// A sibling schema ends this block.
		if indent <= schemaIndent {
			break
		}

		if indent != propertiesIndent || strings.TrimSpace(line) != "properties:" {
			continue
		}

		for j := i + 1; j < len(lines); j++ {
			child := lines[j]

			if strings.TrimSpace(child) == "" || strings.HasPrefix(strings.TrimSpace(child), "#") {
				continue
			}

			childIndent := len(child) - len(strings.TrimLeft(child, " "))

			if childIndent <= propertiesIndent {
				break
			}

			if childIndent != keyIndent {
				continue
			}

			name, _, found := strings.Cut(strings.TrimSpace(child), ":")
			if !found {
				continue
			}

			properties = append(properties, name)
		}

		break
	}

	if len(properties) == 0 {
		t.Fatalf("%s: schema %q has no readable properties; refusing to pass vacuously",
			specPath, schema)
	}

	sort.Strings(properties)

	return properties
}

// jsonKeysOf marshals a value and returns its top-level JSON keys, sorted.
func jsonKeysOf(t *testing.T, v any) []string {
	t.Helper()

	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal %T: %v", v, err)
	}

	keys := make([]string, 0)

	for k := range decodeObject(t, raw) {
		keys = append(keys, k)
	}

	sort.Strings(keys)

	return keys
}

// TestSpecJobSchemaMatchesResponse is the acceptance criterion "the interface
// document agrees with the real response", checked mechanically.
func TestSpecJobSchemaMatchesResponse(t *testing.T) {
	t.Parallel()

	documented := specSchemaProperties(t, "Job")
	actual := jsonKeysOf(t, NewJobView(populatedJob()))

	if strings.Join(documented, ",") != strings.Join(actual, ",") {
		t.Fatalf("spec Job properties %v do not match the response keys %v", documented, actual)
	}
}

// TestSpecJobDataSchemaMatchesResponse covers the nested object, which is where
// the max_time_seconds addition and the extra_reviews omission lived.
func TestSpecJobDataSchemaMatchesResponse(t *testing.T) {
	t.Parallel()

	documented := specSchemaProperties(t, "JobData")
	actual := jsonKeysOf(t, NewJobDataView(populatedJob().Data))

	if strings.Join(documented, ",") != strings.Join(actual, ",") {
		t.Fatalf("spec JobData properties %v do not match the response keys %v", documented, actual)
	}
}

// TestSpecRequestSchemaCoversEveryAcceptedField guards the request direction:
// a field the server stores but the document omits is how extra_reviews went
// undocumented in the first place.
func TestSpecRequestSchemaCoversEveryAcceptedField(t *testing.T) {
	t.Parallel()

	documented := specSchemaProperties(t, "ApiScrapeRequest")

	// Everything JobData stores must be describable on the way in, plus name.
	stored := jsonKeysOf(t, populatedJob().Data)
	required := append([]string{"name"}, stored...)

	have := make(map[string]struct{}, len(documented))
	for _, key := range documented {
		have[key] = struct{}{}
	}

	for _, key := range required {
		if _, ok := have[key]; !ok {
			t.Fatalf("ApiScrapeRequest does not document %q, but the server accepts and stores it", key)
		}
	}

	// The two budget fields must both be documented, with the units spelled
	// out, because getting them the wrong way round is the whole reason A3
	// exists.
	for _, key := range []string{"max_time", "max_time_seconds", "extra_reviews"} {
		if _, ok := have[key]; !ok {
			t.Fatalf("ApiScrapeRequest is missing %q", key)
		}
	}
}

// TestSpecJobDataDocumentsBothTimeUnits pins the unit documentation itself: a
// reader must be able to tell seconds from nanoseconds without reading the code.
func TestSpecJobDataDocumentsBothTimeUnits(t *testing.T) {
	t.Parallel()

	data, err := os.ReadFile(specPath)
	if err != nil {
		t.Fatalf("read %s: %v", specPath, err)
	}

	document := strings.ReplaceAll(string(data), "\r\n", "\n")

	for _, want := range []string{
		"nanoseconds",
		"seconds",
		"max_time_seconds",
		"format=legacy",
		"application/vnd.gms.legacy+json",
		"extra_reviews",
	} {
		if !strings.Contains(document, want) {
			t.Fatalf("%s does not mention %q", specPath, want)
		}
	}
}
