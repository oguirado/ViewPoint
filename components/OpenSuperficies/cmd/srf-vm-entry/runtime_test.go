package main

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProfileAtomicAndJournal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "disk.ext4")
	var calls []string
	run := func(_ context.Context, name string, args ...string) error {
		calls = append(calls, name+" "+strings.Join(args, " "))
		if name == "mke2fs" {
			if _, e := os.Lstat(path); !os.IsNotExist(e) {
				t.Fatal("published before format")
			}
			st, e := os.Stat(args[len(args)-1])
			if e != nil || st.Size() != 64*1024*1024 {
				t.Fatal(st, e)
			}
			writeProfileHeader(t, args[len(args)-1], bareProfileHeader())
		}
		return nil
	}
	if e := prepareProfile(context.Background(), path, 64, run); e != nil {
		t.Fatal(e)
	}
	if len(calls) != 2 || !strings.Contains(calls[0], "-O has_journal") || !strings.Contains(calls[0], "-L srf-profile") || !strings.HasPrefix(calls[1], "e2fsck -p ") {
		t.Fatal(calls)
	}
	calls = nil
	if e := prepareProfile(context.Background(), path, 64, run); e != nil {
		t.Fatal(e)
	}
	if len(calls) != 1 || !strings.HasPrefix(calls[0], "e2fsck") {
		t.Fatal("reformatted existing", calls)
	}
}
func TestProfileFailureNeverPublishes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk.ext4")
	if e := prepareProfile(context.Background(), path, 64, func(context.Context, string, ...string) error { return fmt.Errorf("fake format failed") }); e == nil {
		t.Fatal("format error accepted")
	}
	if _, e := os.Stat(path); !os.IsNotExist(e) {
		t.Fatal("published failed disk")
	}
	if e := os.Symlink("missing", path); e != nil {
		t.Fatal(e)
	}
	if e := prepareProfile(context.Background(), path, 64, func(context.Context, string, ...string) error { t.Fatal("followed symlink"); return nil }); e == nil {
		t.Fatal("accepted symlink")
	}
}
func TestGuestManifest(t *testing.T) {
	dir := t.TempDir()
	data := []byte("offline fake guest")
	sha := fmt.Sprintf("%x", sha256.Sum256(data))
	manifest := sha + "  Image\n" + sha + "  root.raw\n"
	for _, name := range []string{"Image", "root.raw"} {
		if e := os.WriteFile(filepath.Join(dir, name), data, 0600); e != nil {
			t.Fatal(e)
		}
	}
	p := filepath.Join(dir, "MANIFEST.sha256")
	_ = os.WriteFile(p, []byte(manifest), 0600)
	if e := verifyGuest(dir); e != nil {
		t.Fatal(e)
	}
	_ = os.WriteFile(filepath.Join(dir, "root.raw"), []byte("changed"), 0600)
	if e := verifyGuest(dir); e == nil {
		t.Fatal("changed guest accepted")
	}
	_ = os.WriteFile(p, []byte(manifest+sha+"  ../outside\n"), 0600)
	if e := verifyGuest(dir); e == nil {
		t.Fatal("outside manifest accepted")
	}
}

func TestGuestManifestExactBoundedEntries(t *testing.T) {
	dir := t.TempDir()
	data := []byte("offline manifest fixture")
	sha := fmt.Sprintf("%x", sha256.Sum256(data))
	image, root := sha+"  Image\n", sha+"  root.raw\n"
	for _, name := range []string{"Image", "root.raw"} {
		if err := os.WriteFile(filepath.Join(dir, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	valid := image + root
	tests := []struct {
		name, text string
		accept     bool
	}{
		{"valid", valid, true},
		{"at limit", strings.TrimSuffix(valid, "\n") + strings.Repeat(" ", 4096-len(valid)+1), true},
		{"one byte over", strings.TrimSuffix(valid, "\n") + strings.Repeat(" ", 4097-len(valid)+1), false},
		{"hidden trailing entry", strings.TrimSuffix(valid, "\n") + strings.Repeat(" ", 4097-len(valid)+1) + "\n" + sha + "  extra\n", false},
		{"image only", image, false}, {"root only", root, false},
		{"duplicate image", image + image, false}, {"duplicate root", root + root, false},
		{"extra entry", valid + sha + "  extra\n", false},
		{"duplicate third", valid + image, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := os.WriteFile(filepath.Join(dir, "MANIFEST.sha256"), []byte(tt.text), 0600); err != nil {
				t.Fatal(err)
			}
			err := verifyGuest(dir)
			if (err == nil) != tt.accept {
				t.Fatalf("accepted=%v want %v: %v", err == nil, tt.accept, err)
			}
		})
	}
}

// Header fixtures exercise the host check without mounting or formatting a disk.
func bareProfileHeader() []byte {
	b := make([]byte, 8192)
	b[1080], b[1081] = 0x53, 0xef
	copy(b[1024+0x78:1024+0x88], "srf-profile")
	return b
}
func writeProfileHeader(t *testing.T, path string, b []byte) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err = f.WriteAt(b, 0); err != nil {
		t.Fatal(err)
	}
}
func TestProfileRejectsPartitionTablesAndInvalidExt4(t *testing.T) {
	tests := []struct {
		name   string
		alter  func([]byte)
		accept bool
	}{
		{"labelled bare ext4", func([]byte) {}, true},
		{"GPT 512 with root PARTUUID", func(b []byte) {
			copy(b[512:], "EFI PART")
			copy(b[1040:], []byte{0x00, 0xee, 0xff, 0xc0, 0x1e, 0x5a, 0x0e, 0x4d, 0x9e, 0x00, 0x0e, 0x5f, 0x0a, 0x70, 0xc0, 0x01})
		}, false},
		{"GPT 4096", func(b []byte) { copy(b[4096:], "EFI PART") }, false},
		{"wrong label", func(b []byte) { b[1144] = 'x' }, false},
		{"label suffix", func(b []byte) { b[1155] = 'x' }, false},
		{"wrong magic", func(b []byte) { b[1080] = 0 }, false},
		{"MBR signature without partition", func(b []byte) { b[510], b[511] = 0x55, 0xaa }, true},
	}
	for entry := 0; entry < 4; entry++ {
		entry := entry
		tests = append(tests, struct {
			name   string
			alter  func([]byte)
			accept bool
		}{
			fmt.Sprintf("MBR partition %d", entry), func(b []byte) { b[510], b[511] = 0x55, 0xaa; b[446+16*entry+4] = 0x83 }, false,
		})
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "disk.ext4")
			f, err := os.Create(path)
			if err != nil {
				t.Fatal(err)
			}
			if err = f.Truncate(64 * 1024 * 1024); err != nil {
				t.Fatal(err)
			}
			f.Close()
			b := bareProfileHeader()
			tt.alter(b)
			writeProfileHeader(t, path, b)
			calls := 0
			err = prepareProfile(context.Background(), path, 64, func(_ context.Context, name string, _ ...string) error {
				calls++
				if name != "e2fsck" {
					t.Fatalf("unexpected command: %s", name)
				}
				return nil
			})
			if tt.accept {
				if err != nil || calls != 1 {
					t.Fatalf("valid profile: %v, calls=%d", err, calls)
				}
				return
			}
			var refusal *proto.Error
			if !errors.As(err, &refusal) || refusal.Code != 6 || !strings.HasPrefix(err.Error(), "AUSENTE —") {
				t.Fatalf("want refusal rc 6, got %v", err)
			}
			if calls != 0 {
				t.Fatalf("unsafe profile reached fsck: %d", calls)
			}
		})
	}
}

func TestProfileFormatInvalidHeaderNeverPublishes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk.ext4")
	err := prepareProfile(context.Background(), path, 64, func(_ context.Context, name string, _ ...string) error {
		if name != "mke2fs" {
			t.Fatalf("invalid format reached %s", name)
		}
		return nil
	})
	var refusal *proto.Error
	if !errors.As(err, &refusal) || refusal.Code != 6 {
		t.Fatalf("want rc 6, got %v", err)
	}
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatalf("invalid format published: %v", err)
	}
}

func TestProfileRecheckedAfterFsck(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk.ext4")
	err := prepareProfile(context.Background(), path, 64, func(_ context.Context, name string, args ...string) error {
		if name == "mke2fs" {
			writeProfileHeader(t, args[len(args)-1], bareProfileHeader())
		}
		if name == "e2fsck" {
			b := bareProfileHeader()
			copy(b[4096:], "EFI PART")
			writeProfileHeader(t, path, b)
		}
		return nil
	})
	var refusal *proto.Error
	if !errors.As(err, &refusal) || refusal.Code != 6 {
		t.Fatalf("post-fsck partition table accepted: %v", err)
	}
}
