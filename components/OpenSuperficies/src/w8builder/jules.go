package w8builder

import (
	"fmt"
	"sort"
	"strings"
)

// JulesSetup generates the pins-only version preflight shared with Jules.
func JulesSetup(pinsJSON []byte) ([]byte, error) {
	p, e := parseObject(pinsJSON)
	if e != nil {
		return nil, e
	}
	tools, ok := p["tools"].([]any)
	if !ok {
		return nil, refusal(2, "pin", "tools")
	}
	checks := map[string]string{"go": "go version", "tini": "tini --version", "node": "node --version", "rust": "rustc --version", "cargo": "cargo --version", "python3": "python3 --version", "pip": "python3 -m pip --version", "cmake": "cmake --version", "pkg-config": "pkg-config --version", "godot": "godot --version", "blender": "blender -b --version", "build-essential": "dpkg-query -W build-essential", "python3-venv": "python3 --version", "rust/cargo": "rustc --version", "python3-venv/pip": "python3 -m pip --version", "build-essential/cmake/pkg-config": "cmake --version"}
	type check struct{ name, version, cmd string }
	var sorted []check
	for _, v := range tools {
		m, ok := v.(map[string]any)
		if !ok {
			return nil, refusal(2, "pin", "tool")
		}
		n, _ := m["name"].(string)
		version, _ := m["version"].(string)
		cmd, ok := checks[n]
		if !ok {
			return nil, refusal(2, "pin", "unknown Jules tool: "+n)
		}
		if version == "" || strings.ContainsAny(version, "'\n\r") {
			return nil, refusal(2, "pin", "tool version")
		}
		sorted = append(sorted, check{n, version, cmd})
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].name < sorted[j].name })
	var b strings.Builder
	b.WriteString("#!/bin/sh\ntrap 'rc=$?; [ \"$rc\" -eq 1 ] && exit 5; exit \"$rc\"' EXIT\nset -eu\n# Generated from pins.json; do not edit.\n")
	for _, v := range sorted {
		fmt.Fprintf(&b, "%s 2>&1 | grep -F -- '%s' >/dev/null\n", v.cmd, v.version)
	}
	return []byte(b.String()), nil
}
