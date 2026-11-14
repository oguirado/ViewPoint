package capture

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// LinearFile is the dense, render_index-ordered frame file next to raw.json.
const LinearFile = "frames.linear.raw"

// RawJSON is the raw.json of a linearised capture (law2 GuiAdapter raw layout: width, height, stride, file).
type RawJSON struct {
	Width  int    `json:"width"`
	Height int    `json:"height"`
	Stride int    `json:"stride"`
	File   string `json:"file"`
}

// WriteRawJSON linearises the resident frames by render_index into frames.linear.raw (ring slots wrap, so file order
// is not capture order) and writes raw.json beside it. Both stay inside the ramws run dir of the ring.
func (r *Ring) WriteRawJSON() (RawJSON, error) {
	if r.f == nil || len(r.frames) == 0 {
		return RawJSON{}, gate("raw_no_frames")
	}
	order := make([]FrameMeta, len(r.frames))
	copy(order, r.frames)
	sort.SliceStable(order, func(a, b int) bool { return order[a].RenderIndex < order[b].RenderIndex })
	fb := r.frameBytes()
	lin, err := os.OpenFile(filepath.Join(r.Dir, LinearFile), os.O_WRONLY|os.O_CREATE|os.O_TRUNC, FileMode)
	if err != nil {
		return RawJSON{}, ioErr("raw_open", err)
	}
	if err := lin.Chmod(FileMode); err != nil {
		lin.Close()
		return RawJSON{}, ioErr("raw_chmod", err)
	}
	buf := make([]byte, fb)
	for _, f := range order {
		if _, err := r.f.ReadAt(buf, f.RawOffset); err != nil {
			lin.Close()
			return RawJSON{}, ioErr("raw_read", err)
		}
		if _, err := lin.Write(buf); err != nil {
			lin.Close()
			return RawJSON{}, ioErr("raw_write", err)
		}
	}
	if err := lin.Close(); err != nil {
		return RawJSON{}, ioErr("raw_close", err)
	}
	rj := RawJSON{Width: r.W, Height: r.H, Stride: r.W * 3, File: LinearFile}
	body, err := marshalRawJSON(rj)
	if err != nil {
		return RawJSON{}, err
	}
	if err := writeFileMode(filepath.Join(r.Dir, "raw.json"), body, FileMode); err != nil {
		return RawJSON{}, ioErr("raw_json_write", err)
	}
	return rj, nil
}

// writeFileMode is os.WriteFile with the mode set explicitly, so the umask cannot narrow it.
func writeFileMode(path string, body []byte, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if err := f.Chmod(mode); err != nil {
		f.Close()
		return err
	}
	if _, err := f.Write(body); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func marshalRawJSON(rj RawJSON) ([]byte, error) {
	if !plainBasename(rj.File) {
		return nil, schemaErr("raw_file", nil)
	}
	return []byte(`{"file":` + strconv.Quote(rj.File) + `,"height":` + strconv.Itoa(rj.Height) + `,"stride":` + strconv.Itoa(rj.Stride) + `,"width":` + strconv.Itoa(rj.Width) + "}\n"), nil
}

// ValidateRawJSON is the closed validator of raw.json (unknown field, float, bad geometry or path -> E_SCHEMA).
func ValidateRawJSON(raw []byte) error {
	var rj RawJSON
	if err := strict(raw, &rj); err != nil {
		return err
	}
	bad := func(why string) error { return schemaErr(why, nil) }
	if rj.Width < 1 || rj.Width > MaxDim || rj.Height < 1 || rj.Height > MaxDim || rj.Stride < rj.Width*3 {
		return bad("raw_geometry")
	}
	if !plainBasename(rj.File) {
		return bad("raw_file")
	}
	return nil
}

// plainBasename accepts [A-Za-z0-9_][A-Za-z0-9._-]* without "..": no separator, quote, control char or parent path.
func plainBasename(f string) bool {
	if f == "" || strings.Contains(f, "..") {
		return false
	}
	for i := 0; i < len(f); i++ {
		c := f[i]
		alnum := c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'
		if !(alnum || c == '_' || (i > 0 && (c == '.' || c == '-'))) {
			return false
		}
	}
	return true
}
