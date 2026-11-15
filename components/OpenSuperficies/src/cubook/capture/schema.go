package capture

import (
	"bytes"
	"encoding/json"
	"io"
	"regexp"
)

var (
	hex64     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	scopeName = regexp.MustCompile(`^[a-z0-9_]{1,48}$`)
)

// Manifest is vqa.frame/v1 with storage rgb24_raw (field set of visioncore/frame.py plus raw_path/raw_offset).
type Manifest struct {
	Schema      string      `json:"schema"`
	Width       int         `json:"width"`
	Height      int         `json:"height"`
	Channels    int         `json:"channels"`
	PixelFormat string      `json:"pixel_format"`
	Count       int         `json:"count"`
	Storage     string      `json:"storage"`
	RawPath     string      `json:"raw_path"`
	Scope       Scope       `json:"scope"`
	Frames      []FrameMeta `json:"frames"`
}

// Scope tags app and scene.
type Scope struct {
	App   string `json:"app"`
	Scene string `json:"scene"`
}

// Manifest returns the vqa.frame/v1 manifest of the frames resident in the ring.
func (r *Ring) Manifest(app, scene string) Manifest {
	fr := make([]FrameMeta, len(r.frames))
	copy(fr, r.frames)
	return Manifest{Schema: "vqa.frame/v1", Width: r.W, Height: r.H, Channels: 3, PixelFormat: "rgb24",
		Count: len(fr), Storage: "rgb24_raw", RawPath: "frames.raw", Scope: Scope{App: app, Scene: scene}, Frames: fr}
}

func strict(raw []byte, v any) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return schemaErr("decode", err)
	}
	if _, err := d.Token(); err != io.EOF {
		return schemaErr("trailing", nil)
	}
	return nil
}

// ValidateManifestV1 is the closed validator (unknown field, float, bad enum -> E_SCHEMA).
func ValidateManifestV1(raw []byte) error {
	var m Manifest
	if err := strict(raw, &m); err != nil {
		return err
	}
	bad := func(why string) error { return schemaErr(why, nil) }
	if m.Schema != "vqa.frame/v1" || m.Channels != 3 || m.PixelFormat != "rgb24" || m.Storage != "rgb24_raw" {
		return bad("manifest_enum")
	}
	if m.Width < 1 || m.Width > MaxDim || m.Height < 1 || m.Height > MaxDim || m.RawPath == "" || m.RawPath[0] == '/' {
		return bad("manifest_geometry")
	}
	if m.Count < 1 || m.Count != len(m.Frames) || !scopeName.MatchString(m.Scope.App) || !scopeName.MatchString(m.Scope.Scene) {
		return bad("manifest_count_scope")
	}
	var lastT uint64
	var lastRI int64 = -1
	for k, f := range m.Frames {
		if f.I != k || f.TUS < lastT || int64(f.RenderIndex) <= lastRI || !hex64.MatchString(f.SHA256) || !hex64.MatchString(f.FileSHA256) {
			return bad("manifest_frame")
		}
		lastT, lastRI = f.TUS, int64(f.RenderIndex)
	}
	return nil
}

// XWDHeaderJSON is the xwd_header object of capture_receipt.json and capsule.json.
type XWDHeaderJSON struct {
	HeaderSize  int `json:"header_size"`
	NColors     int `json:"ncolors"`
	BPP         int `json:"bpp"`
	BPL         int `json:"bpl"`
	PixelOffset int `json:"pixel_offset"`
}

// Receipt is os.cu.capture-receipt/v1; nothing in it depends on path, run id or time.
type Receipt struct {
	Schema       string        `json:"schema"`
	DetSHA256    string        `json:"det_sha256"`
	Frames       int           `json:"frames"`
	Geometry     [3]int        `json:"geometry"`
	XWDHeader    XWDHeaderJSON `json:"xwd_header"`
	RAMPeakBytes int64         `json:"ram_peak_bytes"`
	RamwsFSType  string        `json:"ramws_fstype"`
}

// Receipt builds the capture receipt for the frames captured so far.
func (r *Ring) Receipt(h Header, fstype string, peak int64) Receipt {
	return Receipt{Schema: "os.cu.capture-receipt/v1", DetSHA256: r.DetSHA256(), Frames: r.total,
		Geometry:     [3]int{r.W, r.H, 24},
		XWDHeader:    XWDHeaderJSON{HeaderSize: h.HeaderSize, NColors: h.NColors, BPP: h.BitsPerPixel, BPL: h.BytesPerLine, PixelOffset: h.PixelOffset},
		RAMPeakBytes: peak, RamwsFSType: fstype}
}

// Quarantine is the {doors, rc} object of vqa.capsule/v1.
type Quarantine struct {
	Doors map[string]int `json:"doors"`
	RC    int            `json:"rc"`
}

// Capsule is vqa.capsule/v1 (capsule.json).
type Capsule struct {
	Schema        string        `json:"schema"`
	Slot          int           `json:"slot"`
	ContainerID   string        `json:"container_id"`
	ImageDigest   string        `json:"image_digest"`
	RunArgsSHA256 string        `json:"run_args_sha256"`
	IsoSHA256     string        `json:"iso_sha256"`
	Geometry      [3]int        `json:"geometry"`
	Cpuset        string        `json:"cpuset"`
	XWDHeader     XWDHeaderJSON `json:"xwd_header"`
	Quarantine    Quarantine    `json:"quarantine"`
}

func (c Capsule) withSchema() Capsule { c.Schema = "vqa.capsule/v1"; return c }

// MarshalCapsule returns the validated capsule.json bytes.
func MarshalCapsule(c Capsule) ([]byte, error) {
	b, err := json.Marshal(c.withSchema())
	if err != nil {
		return nil, schemaErr("marshal", err)
	}
	if err := ValidateCapsuleV1(b); err != nil {
		return nil, err
	}
	return b, nil
}

// ValidateCapsuleV1 is the closed validator of vqa.capsule/v1.
func ValidateCapsuleV1(raw []byte) error {
	// flat shadow type so that a missing xwd_header/quarantine is detectable (pointers)
	var f struct {
		Schema        string         `json:"schema"`
		Slot          int            `json:"slot"`
		ContainerID   string         `json:"container_id"`
		ImageDigest   string         `json:"image_digest"`
		RunArgsSHA256 string         `json:"run_args_sha256"`
		IsoSHA256     string         `json:"iso_sha256"`
		Geometry      [3]int         `json:"geometry"`
		Cpuset        string         `json:"cpuset"`
		XWDHeader     *XWDHeaderJSON `json:"xwd_header"`
		Quarantine    *Quarantine    `json:"quarantine"`
	}
	if err := strict(raw, &f); err != nil {
		return err
	}
	bad := func(why string) error { return schemaErr(why, nil) }
	if f.Schema != "vqa.capsule/v1" || f.Slot < 0 || f.Slot > 7 || !hex64.MatchString(f.ContainerID) ||
		f.ImageDigest == "" || !hex64.MatchString(f.RunArgsSHA256) || !hex64.MatchString(f.IsoSHA256) ||
		f.Cpuset == "" || f.XWDHeader == nil || f.Quarantine == nil {
		return bad("capsule_fields")
	}
	if f.Geometry[0] < 1 || f.Geometry[1] < 1 || f.Geometry[2] < 1 {
		return bad("capsule_geometry")
	}
	if f.Quarantine.RC != 0 && f.Quarantine.RC != 5 || f.Quarantine.Doors == nil {
		return bad("capsule_quarantine")
	}
	for _, v := range f.Quarantine.Doors {
		if v != 0 && v != 1 {
			return bad("capsule_door")
		}
	}
	return nil
}

// Door is the subset of quarantineoracle.PuertaResultado the capsule needs.
type Door struct {
	Puerta string
	Verde  bool
}

// QuarantineFromDoors maps door results to {doors:{"A":0|1,...}, rc:0|5}.
func QuarantineFromDoors(ds []Door) Quarantine {
	q := Quarantine{Doors: map[string]int{}}
	for _, d := range ds {
		if d.Verde {
			q.Doors[d.Puerta] = 1
		} else {
			q.Doors[d.Puerta] = 0
			q.RC = 5
		}
	}
	return q
}
