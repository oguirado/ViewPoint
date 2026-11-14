package w8builder

import (
	"archive/tar"
	"bytes"
	"io"
	"testing"
)

func TestOCIHashesActualBytesAndRuntimeConfiguration(t *testing.T) {
	b, _ := fixtureOCI(t, 0)
	if _, e := hashOCI(bytes.NewReader(b)); e != nil {
		t.Fatal(e)
	}
	tr := tar.NewReader(bytes.NewReader(b))
	files := map[string][]byte{}
	for {
		h, e := tr.Next()
		if e == io.EOF {
			break
		}
		if e != nil {
			t.Fatal(e)
		}
		v, e := io.ReadAll(tr)
		if e != nil {
			t.Fatal(e)
		}
		files[h.Name] = v
	}
	for n, v := range files {
		if bytes.Equal(v, []byte("test fixture layer")) {
			v[0] ^= 1
			files[n] = v
			break
		}
	}
	if _, e := hashOCI(bytes.NewReader(testTar(t, files))); ExitCode(e) != 5 {
		t.Fatalf("corrupt blob rc%d %v", ExitCode(e), e)
	}
}
