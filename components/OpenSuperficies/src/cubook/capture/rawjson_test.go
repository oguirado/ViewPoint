package capture

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
)

func rawRing(t *testing.T, w, h, n int) (*Ring, string) {
	t.Helper()
	if err := os.MkdirAll("/dev/shm/vqa-ramws-test", 0o700); err != nil {
		t.Skip("no /dev/shm")
	}
	dir, err := os.MkdirTemp("/dev/shm/vqa-ramws-test", "rawjson-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	r := NewRing(dir, w, h)
	t.Cleanup(func() { r.Close() })
	for k := 0; k < n; k++ {
		rgb := bytes.Repeat([]byte{byte(10 + k), byte(20 + 3*k), byte(30 + 7*k)}, w*h)
		if _, err := r.Put(rgb, uint64(1000*k)); err != nil {
			t.Fatal(err)
		}
	}
	return r, dir
}

func TestRawJSONMatchesManifestGeometry(t *testing.T) {
	r, dir := rawRing(t, 8, 4, 3)
	rj, err := r.WriteRawJSON()
	if err != nil {
		t.Fatal(err)
	}
	m := r.Manifest("app", "scene")
	if rj.Width != m.Width || rj.Height != m.Height || rj.Stride != m.Width*3 || rj.File != "frames.linear.raw" {
		t.Fatalf("raw.json %+v vs manifest %dx%d", rj, m.Width, m.Height)
	}
	b, err := os.ReadFile(filepath.Join(dir, "raw.json"))
	if err != nil {
		t.Fatalf("raw.json not written: %v", err)
	}
	if err := ValidateRawJSON(b); err != nil {
		t.Fatal(err)
	}
	var back RawJSON
	if err := json.Unmarshal(b, &back); err != nil || back != rj {
		t.Fatalf("roundtrip %+v %v", back, err)
	}
	st, err := os.Stat(filepath.Join(dir, rj.File))
	if err != nil || st.Size() != int64(m.Count*rj.Stride*rj.Height) {
		t.Fatalf("linear file size %v err %v want %d", st, err, m.Count*rj.Stride*rj.Height)
	}
	if ValidateRawJSON([]byte(`{"width":8,"height":4,"stride":24,"file":"f","extra":1}`)) == nil {
		t.Fatal("unknown field accepted")
	}
	if ValidateRawJSON([]byte(`{"width":8,"height":4,"stride":24,"file":"../f"}`)) == nil {
		t.Fatal("parent path accepted")
	}
	if ValidateRawJSON([]byte(`{"width":8,"height":4,"stride":23,"file":"f"}`)) == nil {
		t.Fatal("stride < width*3 accepted")
	}
}

func TestRawLinearisedByRenderIndex(t *testing.T) {
	r, dir := rawRing(t, 8, 4, 6) // 6 puts into 4 slots: ring order is slot-wrapped
	rj, err := r.WriteRawJSON()
	if err != nil {
		t.Fatal(err)
	}
	m := r.Manifest("app", "scene")
	if m.Count != RingSlots {
		t.Fatalf("count %d", m.Count)
	}
	lin, err := os.ReadFile(filepath.Join(dir, rj.File))
	if err != nil {
		t.Fatalf("linear file: %v", err)
	}
	fb := rj.Stride * rj.Height
	if len(lin) != m.Count*fb {
		t.Fatalf("len %d want %d", len(lin), m.Count*fb)
	}
	for k, f := range m.Frames {
		sum := sha256.Sum256(lin[k*fb : (k+1)*fb])
		if hex.EncodeToString(sum[:]) != f.SHA256 {
			t.Fatalf("linear frame %d (render_index %d) sha differs from manifest", k, f.RenderIndex)
		}
		if k > 0 && f.RenderIndex <= m.Frames[k-1].RenderIndex {
			t.Fatalf("manifest not ascending at %d", k)
		}
	}
	if m.Frames[0].RawOffset == 0 {
		t.Fatalf("test needs a wrapped ring (oldest frame at non-zero slot)")
	}
}

func TestRawJSONFileMustBePlainBasename(t *testing.T) {
	for _, f := range []string{"../f", "a..b", "..", "/abs", "a/b", `a\\"b`, `a\\\\b`, `a\\nb`, `a\\u0000b`, `a\\u007fb`, "", ".hidden", "é"} {
		raw := []byte(`{"width":8,"height":4,"stride":24,"file":"` + f + `"}`)
		if ValidateRawJSON(raw) == nil {
			t.Fatalf("file %q accepted", f)
		}
	}
	if err := ValidateRawJSON([]byte(`{"width":8,"height":4,"stride":24,"file":"frames.linear.raw"}`)); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{`a"b`, "a/b", "a\nb", "../x"} {
		if _, err := marshalRawJSON(RawJSON{Width: 8, Height: 4, Stride: 24, File: f}); err == nil {
			t.Fatalf("marshal accepted %q", f)
		}
	}
}

// aclMask returns the ACL_MASK perm bits of path from system.posix_acl_access (getfacl "mask::" line), ok=false
// when the file carries no extended ACL.
func aclMask(t *testing.T, path string) (uint16, bool) {
	t.Helper()
	buf := make([]byte, 1024)
	n, err := syscall.Getxattr(path, "system.posix_acl_access", buf)
	if err != nil || n < 4 {
		return 0, false
	}
	for off := 4; off+8 <= n; off += 8 {
		if binary.LittleEndian.Uint16(buf[off:]) == 0x10 { // ACL_MASK
			return binary.LittleEndian.Uint16(buf[off+2:]), true
		}
	}
	return 0, false
}

// FX2 ADC2-W3-02: frames.raw, frames.linear.raw and raw.json are 0o640 whatever the umask (umask 077 is the worst
// case), so a shared run dir with a default ACL keeps mask r-- (getfacl "#effective:r--"), never mask::---.
func TestRingRawFilesMode0640AndACLMaskReadable(t *testing.T) {
	old := syscall.Umask(0o077)
	defer syscall.Umask(old)
	r, dir := rawRing(t, 4, 2, 2)
	if _, err := r.WriteRawJSON(); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"frames.raw", LinearFile, "raw.json"} {
		st, err := os.Stat(filepath.Join(dir, f))
		if err != nil {
			t.Fatal(err)
		}
		if st.Mode().Perm() != 0o640 {
			t.Fatalf("%s mode %o, want 640", f, st.Mode().Perm())
		}
	}
	// ACL leg: a default ACL naming our own uid (stand-in for share_with_uid) on a fresh run dir.
	if _, err := exec.LookPath("setfacl"); err != nil {
		t.Log("setfacl absent: ACL leg skipped, mode leg holds")
		return
	}
	acl, err := os.MkdirTemp("/dev/shm/vqa-ramws-test", "rawacl-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(acl) })
	if out, err := exec.Command("setfacl", "-d", "-m", "u:"+strconv.Itoa(os.Getuid())+":rwx", acl).CombinedOutput(); err != nil {
		t.Logf("setfacl unsupported here (%v %s): ACL leg skipped", err, out)
		return
	}
	ra := NewRing(acl, 4, 2)
	t.Cleanup(func() { ra.Close() })
	if _, err := ra.Put(bytes.Repeat([]byte{1, 2, 3}, 8), 0); err != nil {
		t.Fatal(err)
	}
	if _, err := ra.WriteRawJSON(); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"frames.raw", LinearFile, "raw.json"} {
		m, ok := aclMask(t, filepath.Join(acl, f))
		if !ok {
			t.Fatalf("%s: no ACL mask under a default ACL", f)
		}
		if m&4 == 0 || m&1 != 0 {
			t.Fatalf("%s: ACL mask %o, want r without x (r-- or rw-)", f, m)
		}
	}
}
