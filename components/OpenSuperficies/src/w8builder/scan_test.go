package w8builder

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"os"
	"strings"
	"testing"
)

// pemBegin is split so secret scanners do not flag these synthetic fixtures.
const pemBegin = "-----" + "BEGIN "

const fixtureRunner = "\x7fELF inert runner fixture"

func init() {
	// Unit runners never build images: use a measured hash of inert ELF fixture bytes.
	defaultRunnerPins = func() map[string]string {
		pins := map[string]string{}
		for _, v := range runnerVerbs {
			pins["opt/w8/"+v] = digest([]byte(fixtureRunner))
		}
		return pins
	}
}
func testTar(t *testing.T, files map[string][]byte) []byte {
	// Generic layer fixtures model the complete B3 image; explicit runner cases
	// control their own entries to test missing, duplicate and altered programs.
	include := true
	for name := range files {
		if strings.HasPrefix(name, "opt/w8/") || strings.HasSuffix(name, ".tar") || name == "oci-layout" || name == "index.json" {
			include = false
		}
	}
	if include {
		copyFiles := map[string][]byte{}
		for name, b := range files {
			copyFiles[name] = b
		}
		for _, v := range runnerVerbs {
			copyFiles["opt/w8/"+v] = []byte(fixtureRunner)
		}
		files = copyFiles
	}

	t.Helper()
	var b bytes.Buffer
	w := tar.NewWriter(&b)
	for n, v := range files {
		if err := w.WriteHeader(&tar.Header{Name: n, Mode: 0644, Size: int64(len(v))}); err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write(v); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}
func TestLayerScannerEnforcesByteCapAndRetentionPrunesByID(t *testing.T) {
	safe := testTar(t, map[string][]byte{"usr/bin/tool": []byte("safe binary")})
	if err := ScanLayers(bytes.NewReader(safe), int64(len(safe))); err != nil {
		t.Fatal(err)
	}
	if ExitCode(ScanLayers(bytes.NewReader(safe), int64(len(safe)-1))) != 5 {
		t.Fatal("cap bypass")
	}
	for _, n := range []string{"workspace/repo.go", "opt/w8/gate", "root/.ssh/key", "opt/cache/mod"} {
		if ExitCode(ScanLayers(bytes.NewReader(testTar(t, map[string][]byte{n: []byte("x")})), 8192)) != 5 {
			t.Fatal(n)
		}
	}
	// A private-key file with a preamble is still a key file.
	data := []byte("Bag Attributes\n    localKeyID: 01\n" + pemBegin + "PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n")
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, map[string][]byte{"etc/ssl/private/site.key": data})), 1<<20)) != 5 {
		t.Fatal("key file accepted")
	}
}

// Conductor ruling (W8-B3 live build #6): byte patterns inside vendor binaries and vendor assets are not
// frames or secrets; real key files, XWD frames and media outside the vendor trees are.
func TestScanPayloadPolicyMatrix(t *testing.T) {
	png := []byte("\x89PNG\r\n\x1a\n fixture image")
	key := []byte(pemBegin + "RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n")
	xwd := make([]byte, 120)
	for i, v := range []uint32{100, 7, 2, 24, 1280, 720} {
		binary.BigEndian.PutUint32(xwd[i*4:], v)
	}
	for _, tc := range []struct {
		name string
		data []byte
		want int
	}{
		{"usr/lib/aarch64-linux-gnu/libgnutls.so.30", append([]byte("\x7fELF\x00\x00"), []byte(pemBegin+"PRIVATE KEY-----")...), 0},
		{"opt/toolchains/node/bin/node", []byte("\x7fELF\x00\x00 ftyp \xff\xd8\xff \x89PNG\r\n\x1a\n"), 0},
		{"usr/share/blender/images/fabric.png", png, 0},
		{"opt/toolchains/go/misc/gopher.png", png, 0},
		{"opt/toolchains/go/src/crypto/tls/testdata/example-key.pem", key, 0},
		{"usr/lib/locale/C.utf8/LC_NAME", []byte{0, 0, 0, 0, 0, 0, 0, 7, 9, 9, 9, 9}, 0},
		{"etc/ssl/private/snakeoil.key", key, 5},
		{"usr/share/doc/leak.pem", key, 5},
		{"var/lib/frames/f0001.png", png, 5},
		{"etc/capture.mp4", []byte("\x00\x00\x00\x18ftypmp42 fixture"), 5},
		{"usr/share/capture/f.xwd", xwd, 5},
		{"var/tmp/f.y4m", []byte("YUV4MPEG2 W1280 H720"), 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := ExitCode(ScanLayers(bytes.NewReader(testTar(t, map[string][]byte{tc.name: tc.data})), 1<<20))
			if got != tc.want {
				t.Fatalf("rc=%d want=%d", got, tc.want)
			}
		})
	}
}

func TestRepoIdentityPolicyPathBoundary(t *testing.T) {
	for _, tc := range []struct {
		name string
		want int
	}{
		{"usr/local/go/src/fmt/print.go", 0}, {"usr/lib/python3.11/site.py", 0},
		{"opt/rust/lib/core.rs", 0}, {"src/copied.txt", 5}, {"work/input", 5},
		{"repo/input", 5}, {"home/worker/input", 5}, {"tmp/input", 5},
		{"usr/share/data/.git", 5}, {"usr/share/data/.git/config", 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := ExitCode(ScanLayers(bytes.NewReader(testTar(t, map[string][]byte{tc.name: []byte("distribution source")})), 8192))
			if got != tc.want {
				t.Fatalf("rc=%d want=%d", got, tc.want)
			}
		})
	}
}

func TestScanLayersFivePinnedBinaries(t *testing.T) {
	// Fake ELF bytes deliberately contain media signatures: exact pinned programs
	// are not frames, while arbitrary binaries remain subject to payload scanning.
	binary := []byte("\x7fELF\x00ftyp\xff\xd8\xff fixture program")
	files := map[string][]byte{}
	pins := map[string]string{}
	for _, verb := range []string{"ingest", "gate", "status", "extract", "purge"} {
		n := "opt/w8/" + verb
		files[n] = binary
		pins[n] = digest(binary)
	}
	p := ScanPolicy{RunnerSHA256: pins}
	if e := ScanLayers(bytes.NewReader(testTar(t, files)), 1<<20, p); e != nil {
		t.Fatal(e)
	}
	files["opt/w8/evil"] = binary
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, files)), 1<<20, p)) != 5 {
		t.Fatal("unlisted runner accepted")
	}
	delete(files, "opt/w8/evil")
	files["opt/w8/gate"] = append(binary, 'x')
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, files)), 1<<20, p)) != 5 {
		t.Fatal("unpinned runner accepted")
	}
}

func TestRunnerPinSetMustBeCompleteAndExact(t *testing.T) {
	binary := []byte("\x7fELF known runner")
	pins := map[string]string{}
	files := map[string][]byte{}
	for _, v := range runnerVerbs {
		n := "opt/w8/" + v
		pins[n] = digest(binary)
		files[n] = binary
	}
	delete(files, "opt/w8/status")
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, files)), 1<<20, ScanPolicy{RunnerSHA256: pins})) != 5 {
		t.Fatal("missing binary accepted")
	}
	files["opt/w8/status"] = binary
	delete(pins, "opt/w8/status")
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, files)), 1<<20, ScanPolicy{RunnerSHA256: pins})) != 5 {
		t.Fatal("partial pins accepted")
	}
}

func TestMeasuredRunnerBinaryScan(t *testing.T) {
	path := os.Getenv("W8_TEST_RUNNER_BIN")
	if path == "" {
		t.Skip("conductor reproducible binary not supplied")
	}
	b, e := os.ReadFile(path)
	if e != nil {
		t.Fatal(e)
	}
	if digest(b) != ApprovedRunnerSHA256 {
		t.Fatal("conductor runner differs from compiled approval")
	}
	files := map[string][]byte{}
	for _, v := range runnerVerbs {
		files["opt/w8/"+v] = b
	}
	if e = ScanLayers(bytes.NewReader(testTar(t, files)), 32<<20, ScanPolicy{RunnerSHA256: runnerPins()}); e != nil {
		t.Fatal(e)
	}
	files["opt/w8/status"] = append(append([]byte{}, b...), 0)
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, files)), 32<<20, ScanPolicy{RunnerSHA256: runnerPins()})) != 5 {
		t.Fatal("altered measured binary passed")
	}
}

// docker save on the containerd image store emits an OCI layout: layers are gzip blobs.
func TestScanLayersOCILayoutGzipBlobs(t *testing.T) {
	binary := []byte("\x7fELF oci runner")
	pins := map[string]string{}
	files := map[string][]byte{}
	for _, v := range runnerVerbs {
		pins["opt/w8/"+v] = digest(binary)
		files["opt/w8/"+v] = binary
	}
	gz := func(m map[string][]byte) []byte {
		var b bytes.Buffer
		w := gzip.NewWriter(&b)
		_, _ = w.Write(testTar(t, m))
		_ = w.Close()
		return b.Bytes()
	}
	good := testTar(t, map[string][]byte{"index.json": []byte(`{"schemaVersion":2}`), "blobs/sha256/aa": gz(files), "blobs/sha256/bb": []byte(`{"config":{}}`)})
	if e := ScanLayers(bytes.NewReader(good), 1<<20, ScanPolicy{RunnerSHA256: pins}); e != nil {
		t.Fatalf("oci layout with runner refused: %v", e)
	}
	files["var/lib/frames/f.png"] = []byte("\x89PNG\r\n\x1a\n frame")
	bad := testTar(t, map[string][]byte{"index.json": []byte(`{}`), "blobs/sha256/aa": gz(files)})
	if ExitCode(ScanLayers(bytes.NewReader(bad), 1<<20, ScanPolicy{RunnerSHA256: pins})) != 5 {
		t.Fatal("frame inside an OCI gzip layer accepted")
	}
	zstd := testTar(t, map[string][]byte{"blobs/sha256/cc": {0x28, 0xb5, 0x2f, 0xfd, 0, 0, 0, 0}})
	if ExitCode(ScanLayers(bytes.NewReader(zstd), 1<<20)) != 5 {
		t.Fatal("zstd layer accepted unscanned")
	}
}

func TestScanImageEntryNamesAndLinks(t *testing.T) {
	layer := func(hdrs ...*tar.Header) []byte {
		var b bytes.Buffer
		w := tar.NewWriter(&b)
		for _, h := range hdrs {
			if h.Typeflag == tar.TypeReg {
				h.Size = 1
			}
			_ = w.WriteHeader(h)
			if h.Typeflag == tar.TypeReg {
				_, _ = w.Write([]byte("x"))
			}
		}
		_ = w.Close()
		return testTar(t, map[string][]byte{"blobs/sha256/aa": b.Bytes()})
	}
	for _, tc := range []struct {
		name string
		h    *tar.Header
		want int
	}{
		{"absolute symlink", &tar.Header{Name: "usr/local/bin/go", Typeflag: tar.TypeSymlink, Linkname: "/opt/toolchains/go/bin/go"}, 0},
		{"relative up symlink", &tar.Header{Name: "usr/lib/x/libz.so", Typeflag: tar.TypeSymlink, Linkname: "../libz.so.1"}, 0},
		{"tilde and dollar names", &tar.Header{Name: "usr/share/perl/x~$y.pm", Typeflag: tar.TypeReg}, 0},
		{"hardlink traversal", &tar.Header{Name: "usr/bin/a", Typeflag: tar.TypeLink, Linkname: "../../etc/shadow"}, 5},
		{"hardlink absolute", &tar.Header{Name: "usr/bin/a", Typeflag: tar.TypeLink, Linkname: "/etc/shadow"}, 5},
		{"entry traversal", &tar.Header{Name: "usr/../../etc/x", Typeflag: tar.TypeReg}, 5},
		{"ssh dir", &tar.Header{Name: "etc/skel/.ssh/id", Typeflag: tar.TypeReg}, 5},
		{"operator token", &tar.Header{Name: "var/lib/operator.token", Typeflag: tar.TypeReg}, 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := ExitCode(ScanLayers(bytes.NewReader(layer(tc.h)), 1<<20)); got != tc.want {
				t.Fatalf("rc=%d want=%d", got, tc.want)
			}
		})
	}
}

// Live build #8 findings (visual-qa-0 harness): base layer "./" entry, whiteouts and the pinned base /root dotfiles.
func TestScanBaseLayerQuirks(t *testing.T) {
	layer := func(entries map[string][]byte, dot bool) []byte {
		var b bytes.Buffer
		w := tar.NewWriter(&b)
		if dot {
			_ = w.WriteHeader(&tar.Header{Name: "./", Typeflag: tar.TypeDir, Mode: 0755})
		}
		for n, d := range entries {
			_ = w.WriteHeader(&tar.Header{Name: n, Typeflag: tar.TypeReg, Size: int64(len(d)), Mode: 0644})
			_, _ = w.Write(d)
		}
		_ = w.Close()
		return testTar(t, map[string][]byte{"blobs/sha256/aa": b.Bytes()})
	}
	if e := ScanLayers(bytes.NewReader(layer(map[string][]byte{"etc/hostname": []byte("x")}, true)), 1<<20); e != nil {
		t.Fatalf("base layer with ./ entry refused: %v", e)
	}
	if e := ScanLayers(bytes.NewReader(layer(map[string][]byte{"root/.wh..bashrc": {}, "root/.wh..profile": {}}, false)), 1<<20); e != nil {
		t.Fatalf("whiteouts refused: %v", e)
	}
	if ExitCode(ScanLayers(bytes.NewReader(layer(map[string][]byte{"root/.bashrc": []byte("export SECRET=1\n")}, false)), 1<<20)) != 5 {
		t.Fatal("modified /root/.bashrc accepted")
	}
	if ExitCode(ScanLayers(bytes.NewReader(layer(map[string][]byte{"root/.wh..x": []byte("not empty")}, false)), 1<<20)) != 5 {
		t.Fatal("non-empty whiteout-named file under /root accepted")
	}
}
