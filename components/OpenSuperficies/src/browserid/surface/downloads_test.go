package surface

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestExportDownloadRefusesPathsOutsideTheDownloadFolder(t *testing.T) {
	tool, _, _, _ := setup(t)
	dir := t.TempDir()
	for _, name := range []string{"../Cookies", "a/b", "", ".", ".."} {
		if _, e := tool.ExportDownload(context.Background(), "srf-id-test", name, dir); Code(e) != 2 {
			t.Fatal(name, e)
		}
	}
	if _, e := tool.ExportDownload(context.Background(), "srf-id-test", "x.zip", "relative/dir"); Code(e) != 2 {
		t.Fatal("destination must be absolute", e)
	}
	if e := os.WriteFile(filepath.Join(dir, "x.zip"), []byte("keep"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := tool.ExportDownload(context.Background(), "srf-id-test", "x.zip", dir); Code(e) != 4 {
		t.Fatal("an existing file is never replaced", e)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "x.zip")); string(b) != "keep" {
		t.Fatal("existing file changed")
	}
}

func TestDeleteDownloadRefusesPathsOutsideTheDownloadFolder(t *testing.T) {
	tool, _, _, _ := setup(t)
	for _, name := range []string{"../Cookies", "a/b", "", ".", ".."} {
		if _, e := tool.DeleteDownload(context.Background(), "srf-id-test", name); Code(e) != 2 {
			t.Fatal(name, e)
		}
	}
}
