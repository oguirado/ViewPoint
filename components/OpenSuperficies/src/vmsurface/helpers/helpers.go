// Package helpers contains the generated surface helper identities and relay policy.
package helpers

//go:generate go run ./internal/gen -surface ../../browserid/surface -out helpers_gen.go

// Entry binds an exact source digest to its execution bounds.
type Entry struct {
	Name      string
	SHA256    string
	TimeoutMS int64
	StdinCap  int64
	Stream    bool
}

// Hashes returns a fresh allow-list; callers cannot mutate the generated table.
func Hashes() map[string]bool {
	m := map[string]bool{}
	for _, e := range Entries {
		m[e.SHA256] = true
	}
	return m
}
