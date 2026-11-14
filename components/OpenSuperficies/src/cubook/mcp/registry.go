package mcp

import (
	"bytes"
	"encoding/json"
	"os"
)

type ToolSpec struct {
	Risk    string `json:"risk"`
	Witness bool   `json:"witness"`
	Via     string `json:"via,omitempty"`
}

type ServerSpec struct {
	Endpoint       string              `json:"endpoint"`
	GuiFallbackOps []string            `json:"gui_fallback_ops"`
	Unlisted       string              `json:"unlisted"`
	Tools          map[string]ToolSpec `json:"tools"`
}

type Registry struct {
	Schema  string                `json:"schema"`
	OZRoots []string              `json:"oz_roots"`
	Servers map[string]ServerSpec `json:"servers"`
}

func (s ServerSpec) guiAllowed(op string) bool {
	for _, o := range s.GuiFallbackOps {
		if o == op {
			return true
		}
	}
	return false
}

// LoadRegistry decodes strictly (unknown field / bad enum -> rc 2).
func LoadRegistry(path string) (*Registry, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, schema("registry unreadable")
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	var r Registry
	if err := dec.Decode(&r); err != nil {
		return nil, schema("registry: " + err.Error())
	}
	if r.Schema != "os.cu.mcp-registry/v1" {
		return nil, schema("registry schema")
	}
	for n, s := range r.Servers {
		if s.Endpoint == "" || (s.Unlisted != "refuse" && s.Unlisted != "confirm") {
			return nil, schema("server " + n)
		}
		for t, ts := range s.Tools {
			if ts.Risk != "read" && ts.Risk != "write" && ts.Risk != "irrev" {
				return nil, schema("tool " + n + "/" + t)
			}
		}
	}
	return &r, nil
}
