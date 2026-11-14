package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProfileDiscoveryByLabel(t *testing.T) {
	for _, name := range []string{"vda", "vdb", "vdc1"} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			sys, dev := filepath.Join(root, "sys"), filepath.Join(root, "dev")
			os.MkdirAll(sys, 0700)
			os.MkdirAll(dev, 0700)
			add := func(n, label string, magic bool) {
				t.Helper()
				if err := os.MkdirAll(filepath.Join(sys, n), 0700); err != nil {
					t.Fatal(err)
				}
				b := make([]byte, 2048)
				if magic {
					b[1080] = 0x53
					b[1081] = 0xef
				}
				copy(b[1144:1160], label)
				if err := os.WriteFile(filepath.Join(dev, n), b, 0600); err != nil {
					t.Fatal(err)
				}
			}
			add("vdd", "other", true)
			add("vde", "srf-profile", false)
			add("vdg", "srf-profile-extra", true)
			if _, err := discoverProfile(sys, dev); err == nil || !strings.HasPrefix(err.Error(), "AUSENTE —") {
				t.Fatalf("missing label: %v", err)
			}
			add(name, "srf-profile", true)
			got, err := discoverProfile(sys, dev)
			if err != nil || got != filepath.Join(dev, name) {
				t.Fatalf("%q %v", got, err)
			}
			add("vdf2", "srf-profile", true)
			if _, err := discoverProfile(sys, dev); err == nil {
				t.Fatal("duplicate accepted")
			}
		})
	}
}

func TestProfileDiscoveryUnreadableRefuses(t *testing.T) {
	root := t.TempDir()
	os.Mkdir(filepath.Join(root, "vda"), 0700)
	if _, err := discoverProfile(root, filepath.Join(root, "absent")); err == nil {
		t.Fatal("unreadable candidate accepted")
	}
}

func TestProfileFailurePreventsMountAndFsck(t *testing.T) {
	ops := fakeBootOps()
	ops.profile = func() (string, error) { return "", fmt.Errorf("AUSENTE — profile not unique") }
	ops.mount = func(string, string, string, string) error { t.Fatal("mounted before label validation"); return nil }
	ops.check = func([]string) error { t.Fatal("fsck before label validation"); return nil }
	if prepareGuest(ops) == nil {
		t.Fatal("profile discovery failure accepted")
	}
}

func TestProfileTruncatedSuperblockRefuses(t *testing.T) {
	root := t.TempDir()
	sys, dev := filepath.Join(root, "sys"), filepath.Join(root, "dev")
	if err := os.MkdirAll(filepath.Join(sys, "vda1"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(dev, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dev, "vda1"), make([]byte, 1080), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := discoverProfile(sys, dev); err == nil {
		t.Fatal("short superblock accepted")
	}
}
