package main

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
)

var virtioBlockName = regexp.MustCompile(`^vd[a-z]+[0-9]*$`)

// discoverProfile inspects both disks and partitions listed by sysfs. Device
// enumeration order cannot select the root disk as the writable profile.
func discoverProfile(sysRoot, devRoot string) (string, error) {
	entries, err := os.ReadDir(sysRoot)
	if err != nil {
		return "", fmt.Errorf("AUSENTE — profile block discovery: %w", err)
	}
	var found string
	for _, entry := range entries {
		if !virtioBlockName.MatchString(entry.Name()) {
			continue
		}
		path := filepath.Join(devRoot, entry.Name())
		f, err := os.Open(path)
		if err != nil {
			return "", fmt.Errorf("AUSENTE — profile device open: %w", err)
		}
		var sb [136]byte
		_, err = f.ReadAt(sb[:], 1024)
		closeErr := f.Close()
		if err != nil {
			return "", fmt.Errorf("AUSENTE — profile superblock read: %w", err)
		}
		if closeErr != nil {
			return "", fmt.Errorf("AUSENTE — profile device close: %w", closeErr)
		}
		if sb[56] != 0x53 || sb[57] != 0xef || !bytes.Equal(sb[120:136], []byte("srf-profile\x00\x00\x00\x00\x00")) {
			continue
		}
		if found != "" {
			return "", fmt.Errorf("AUSENTE — multiple srf-profile filesystems")
		}
		found = path
	}
	if found == "" {
		return "", fmt.Errorf("AUSENTE — srf-profile filesystem missing")
	}
	return found, nil
}
