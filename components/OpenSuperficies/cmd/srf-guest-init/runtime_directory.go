package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
)

type directoryOps struct {
	mkdirAll func(string, os.FileMode) error
	mkdir    func(string, os.FileMode) error
	lstat    func(string) (os.FileInfo, error)
	chmod    func(string, os.FileMode) error
}

func makeGuestDirectory(p string, mode os.FileMode, fs directoryOps) error {
	parent := filepath.Dir(p)
	// Reject existing symlink ancestors before MkdirAll can follow them.
	for a := parent; ; a = filepath.Dir(a) {
		s, err := fs.lstat(a)
		if err != nil && !os.IsNotExist(err) {
			return err
		}
		if err == nil && (!s.IsDir() || s.Mode()&os.ModeSymlink != 0) {
			return errors.New("AUSENTE — unsafe runtime parent")
		}
		if a == filepath.Dir(a) {
			break
		}
	}
	if err := fs.mkdirAll(parent, 0755); err != nil {
		return err
	}
	// Legacy setup may have made /run/user private. Only runtime ancestors
	// under /run/ are repaired; /, /run and private /profile are never chmodded.
	for a := parent; strings.HasPrefix(a, "/run/"); a = filepath.Dir(a) {
		s, err := fs.lstat(a)
		if err != nil {
			return err
		}
		if s.Mode().Perm()&0005 != 0005 {
			if err := fs.chmod(a, 0755); err != nil {
				return err
			}
		}
	}
	if err := fs.mkdir(p, mode); err != nil && !os.IsExist(err) {
		return err
	}
	s, err := fs.lstat(p)
	if err != nil {
		return err
	}
	if !s.IsDir() || s.Mode()&os.ModeSymlink != 0 {
		return errors.New("AUSENTE — unsafe runtime directory")
	}
	return nil
}
