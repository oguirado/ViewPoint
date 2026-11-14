package ramcap

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

func buildXWD(w, h int, patch map[int]uint32, name string) []byte {
	if name == "" {
		name = "Xvfb main window\x00"
	}
	f := [25]uint32{uint32(100 + len(name)), 7, 2, 24, uint32(w), uint32(h), 0, 0, 32, 0, 32, 32, uint32(w * 4), 4, 0xff0000, 0xff00, 0xff, 8, 0, 0, uint32(w), uint32(h), 0, 0, 0}
	for k, v := range patch {
		f[k] = v
	}
	b := make([]byte, int(f[0])+int(f[19])*12+int(f[12])*h)
	for i, v := range f {
		binary.BigEndian.PutUint32(b[i*4:], v)
	}
	copy(b[100:], name)
	for y := range h {
		for x := range w {
			i := int(f[0]) + int(f[19])*12 + y*int(f[12]) + x*4
			r, g, bl := byte(x*37+y), byte(y*53+7), byte(x*y)
			if f[7] == 0 {
				copy(b[i:], []byte{bl, g, r, 0})
			} else {
				copy(b[i:], []byte{0, r, g, bl})
			}
		}
	}
	return b
}
func code(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil || err.Error() != want {
		t.Fatalf("got %v want %s", err, want)
	}
}
func TestGoodVectors(t *testing.T) {
	b, e := os.ReadFile("testdata/d11_xfb_vectors.json")
	if e != nil {
		t.Fatal(e)
	}
	if h := sha256.Sum256(b); hex.EncodeToString(h[:]) != "a737817b280623ee5ba53ba950155ca92ca51abb558cbd638af75e685e81f7c3" {
		t.Fatal("vector pin")
	}
	var v struct {
		Good []struct {
			Name                string
			Width, Height, Size int
			XWD                 string `json:"xwd_sha256"`
			Frame               string `json:"frame_sha256"`
		}
	}
	if e = json.Unmarshal(b, &v); e != nil {
		t.Fatal(e)
	}
	for _, c := range v.Good {
		t.Run(c.Name, func(t *testing.T) {
			p := map[int]uint32{}
			name := ""
			switch c.Name {
			case "msb_8x4":
				p[7] = 1
				p[9] = 1
			case "padded_rows_5x3":
				p[12] = uint32(c.Width*4 + 8)
			case "colormap_256_6x2":
				p[18] = 256
				p[19] = 256
			case "short_name_header_7x3":
				name = "x\x00"
			}
			b := buildXWD(c.Width, c.Height, p, name)
			h, e := Validate(b, c.Width, c.Height)
			if e != nil {
				t.Fatal(e)
			}
			x := sha256.Sum256(b)
			f, e := FrameSHA256(b, h)
			if e != nil {
				t.Fatal(e)
			}
			if hex.EncodeToString(x[:]) != c.XWD || hex.EncodeToString(f[:]) != c.Frame || len(b) != c.Size {
				t.Fatal("vector mismatch")
			}
		})
	}
}
func TestBadVectors(t *testing.T) {
	b, _ := os.ReadFile("testdata/d11_xfb_vectors.json")
	var v struct{ Bad []struct{ Name, Code string } }
	if e := json.Unmarshal(b, &v); e != nil {
		t.Fatal(e)
	}
	patches := map[string]map[int]uint32{"bpp24": {11: 24}, "depth32": {3: 32}, "xy_pixmap": {2: 1}, "pseudocolor": {13: 3}, "swapped_masks": {14: 255, 16: 0xff0000}, "version6": {1: 6}}
	for _, c := range v.Bad {
		t.Run(c.Name, func(t *testing.T) { _, e := Validate(buildXWD(4, 2, patches[c.Name], ""), 0, 0); code(t, e, c.Code) })
	}
}
func TestSizeRules(t *testing.T) {
	b := buildXWD(4, 2, nil, "")
	for _, v := range [][]byte{append(append([]byte{}, b...), 0), b[:len(b)-1]} {
		_, e := Validate(v, 0, 0)
		code(t, e, "E_XWD_SIZE")
	}
}

// DEVIATIONS: Go-only hardening, intentionally absent from the peer vectors.
func TestDeviations(t *testing.T) {
	for _, c := range []struct {
		field int
		value uint32
		code  string
	}{{7, 2, "E_XWD_FORMAT"}, {0, 65537, "E_XWD_GEOMETRY"}, {19, 65537, "E_XWD_GEOMETRY"}, {12, 65540, "E_XWD_GEOMETRY"}} {
		b := buildXWD(4, 2, nil, "")
		binary.BigEndian.PutUint32(b[c.field*4:], c.value)
		_, e := ParseHeader(b)
		code(t, e, c.code)
	}
}
func TestWindowInfoAndCrop(t *testing.T) {
	b := buildXWD(8, 4, nil, "srf-fbread xid=42\x00")
	h, e := Validate(b, 0, 0)
	if e != nil {
		t.Fatal(e)
	}
	win, e := WindowInfo(b, h)
	if e != nil || win.XID != 42 {
		t.Fatal(win, e)
	}
	rgb, e := Crop(b, h, 2, 1, 3, 2)
	if e != nil || len(rgb) != 18 || rgb[0] != 75 {
		t.Fatal(rgb, e)
	}
	_, e = Crop(b, h, -1, 0, 1, 1)
	code(t, e, "E_XWD_GEOMETRY")
	b = buildXWD(8, 4, nil, "srf-fbread xid=4294967296\x00")
	h, _ = Validate(b, 0, 0)
	_, e = WindowInfo(b, h)
	code(t, e, "E_XWD_GEOMETRY")
}
func TestTornRule(t *testing.T) {
	n := 0
	_, e := ConsistentRead(func() ([]byte, error) { n++; return []byte{byte(n % 2)}, nil }, 8)
	code(t, e, "E_TORN")
	if n != 8 {
		t.Fatal(n)
	}
	n = 0
	b, e := ConsistentRead(func() ([]byte, error) {
		n++
		if n == 1 {
			return []byte{1}, nil
		}
		return []byte{2}, nil
	}, 8)
	if e != nil || n != 3 || b[0] != 2 {
		t.Fatal(n, b, e)
	}
}

func TestLsbMsbSameHash(t *testing.T) {
	a := buildXWD(8, 4, nil, "")
	b := buildXWD(8, 4, map[int]uint32{7: 1, 9: 1, 18: 16, 19: 16}, "other header\x00")
	ah, _ := Validate(a, 0, 0)
	bh, _ := Validate(b, 0, 0)
	as, _ := FrameSHA256(a, ah)
	bs, _ := FrameSHA256(b, bh)
	if as != bs {
		t.Fatal("byte-order-dependent pixel digest")
	}
}
