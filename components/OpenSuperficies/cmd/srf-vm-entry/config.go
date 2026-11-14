package main

import (
	"bytes"
	"encoding/json"
	"io"
	"opensuperficies.local/srf/src/vmsurface/launch"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"regexp"
	"strconv"
)

type configuration struct {
	launch.Config
	DiskMiB int
	Boot    map[string]string
}
type tile struct {
	ID string `json:"id"`
	X  int    `json:"x"`
	Y  int    `json:"y"`
	W  int    `json:"w"`
	H  int    `json:"h"`
}

var tileID = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,128}$`)
var digits = regexp.MustCompile(`^[0-9]+$`)

func refused(code int, detail string) error {
	return &proto.Error{Code: code, Detail: "AUSENTE — " + detail}
}
func integer(s string, min, max int) (int, error) {
	n, e := strconv.Atoi(s)
	if e != nil || !digits.MatchString(s) || n < min || n > max {
		return 0, refused(2, "invalid VM resources")
	}
	return n, nil
}
func settings(env map[string]string) (configuration, error) {
	c := configuration{Boot: make(map[string]string)}
	if env["SRF_PROGRAM"] != "notepad-vm" {
		return c, refused(2, "unknown VM program")
	}
	n, e := integer(env["SRF_WINDOWS"], 1, 4)
	if e != nil {
		return c, e
	}
	if len(env["SRF_TILES"]) > 65536 {
		return c, refused(2, "invalid tiles")
	}
	var rawTiles []json.RawMessage
	if json.Unmarshal([]byte(env["SRF_TILES"]), &rawTiles) != nil {
		return c, refused(2, "invalid tiles")
	}
	for _, raw := range rawTiles {
		if !exactTile(raw) {
			return c, refused(2, "invalid tile fields")
		}
	}
	var tiles []tile
	d := json.NewDecoder(bytes.NewBufferString(env["SRF_TILES"]))
	d.DisallowUnknownFields()
	if e = d.Decode(&tiles); e != nil || len(tiles) != n {
		return c, refused(2, "invalid tiles")
	}
	if d.Decode(new(any)) != io.EOF {
		return c, refused(2, "trailing tiles")
	}
	seen := map[string]bool{}
	for i, t := range tiles {
		if !tileID.MatchString(t.ID) || seen[t.ID] || t.X < 0 || t.Y < 0 || t.W < 1 || t.H < 1 || t.W > 1280 || t.H > 900 || t.X > 1280-t.W || t.Y > 900-t.H {
			return c, refused(2, "invalid tile bounds")
		}
		seen[t.ID] = true
		for _, b := range tiles[:i] {
			if t.X < b.X+b.W && b.X < t.X+t.W && t.Y < b.Y+b.H && b.Y < t.Y+t.H {
				return c, refused(2, "overlapping tiles")
			}
		}
	}
	if c.CPUs, e = integer(env["SRF_GUEST_CPUS"], 1, 5); e != nil {
		return c, e
	}
	if c.MemoryMiB, e = integer(env["SRF_GUEST_MEM_MIB"], 512, 7168); e != nil {
		return c, e
	}
	if c.DiskMiB, e = integer(env["SRF_PROFILE_DISK_MIB"], 64, 4096); e != nil {
		return c, e
	}
	if env["NO_AT_BRIDGE"] != "0" && env["NO_AT_BRIDGE"] != "1" {
		return c, refused(2, "invalid accessibility environment")
	}
	for _, k := range []string{"SRF_PROGRAM", "SRF_WINDOWS", "SRF_TILES", "NO_AT_BRIDGE"} {
		c.Boot[k] = env[k]
	}
	c.RuntimeDir = "/run/newton-tenant"
	c.Profile = "/profile/disk.ext4"
	return c, nil
}

// exactTile rejects missing, null, repeated and case-folded JSON fields before
// decoding to Go's zero-valued integer fields.
func exactTile(raw []byte) bool {
	d := json.NewDecoder(bytes.NewReader(raw))
	token, e := d.Token()
	if e != nil || token != json.Delim('{') {
		return false
	}
	seen := map[string]bool{}
	for d.More() {
		token, e = d.Token()
		key, ok := token.(string)
		if e != nil || !ok || seen[key] || (key != "id" && key != "x" && key != "y" && key != "w" && key != "h") {
			return false
		}
		seen[key] = true
		var v json.RawMessage
		if d.Decode(&v) != nil || bytes.Equal(bytes.TrimSpace(v), []byte("null")) {
			return false
		}
	}
	if _, e = d.Token(); e != nil || len(seen) != 5 {
		return false
	}
	return d.Decode(new(any)) == io.EOF
}
