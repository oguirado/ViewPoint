package recstream

import (
	"encoding/binary"
	"errors"
	"io"
	"os"
)

var ErrByteLimit = errors.New("E_RAM_QUOTA: MP4 byte cap")

// mp4Sink admits only top-level boxes that fit the remaining hard budget.
// On a cap/error it rolls back to the last complete mdat (or initial moov),
// removing a trailing moof as well as any incomplete box. Memory is bounded by
// a box header; encoded payload goes directly to the private tmpfs file.
type mp4Sink struct {
	fragments              uint64
	f                      *os.File
	limit, pos, checkpoint int64
	header                 []byte
	remain                 int64
	kind                   string
	limited                bool
	err                    error
}

func newMP4Sink(f *os.File, limit int64) *mp4Sink { return &mp4Sink{f: f, limit: limit} }
func (s *mp4Sink) Write(p []byte) (int, error) {
	if s.err != nil {
		return 0, s.err
	}
	used := 0
	for len(p) > 0 {
		if s.remain == 0 {
			need := 8
			if len(s.header) >= 8 && binary.BigEndian.Uint32(s.header) == 1 {
				need = 16
			}
			n := min(need-len(s.header), len(p))
			s.header = append(s.header, p[:n]...)
			p = p[n:]
			used += n
			if len(s.header) < need {
				continue
			}
			sz := uint64(binary.BigEndian.Uint32(s.header))
			if sz == 1 && need == 8 {
				continue
			}
			if sz == 1 {
				sz = binary.BigEndian.Uint64(s.header[8:])
			}
			if sz < uint64(need) {
				s.err = errors.New("E_MP4: unbounded/invalid box")
				return used, s.err
			}
			if sz > uint64(s.limit-s.pos) {
				s.limited = true
				s.err = ErrByteLimit
				return used, s.err
			}
			s.kind = string(s.header[4:8])
			s.remain = int64(sz) - int64(need)
			if _, err := s.f.Write(s.header); err != nil {
				s.err = err
				return used, err
			}
			s.pos += int64(need)
			s.header = nil
			if s.remain == 0 {
				s.complete()
			}
		} else {
			n := int64(len(p))
			if n > s.remain {
				n = s.remain
			}
			wrote, err := s.f.Write(p[:n])
			used += wrote
			s.pos += int64(wrote)
			s.remain -= int64(wrote)
			p = p[wrote:]
			if err != nil {
				s.err = err
				return used, err
			}
			if int64(wrote) != n {
				s.err = io.ErrShortWrite
				return used, s.err
			}
			if s.remain == 0 {
				s.complete()
			}
		}
	}
	return used, nil
}
func (s *mp4Sink) complete() {
	if s.kind == "mdat" {
		s.fragments++
	}
	if s.kind == "moov" || s.kind == "mdat" {
		s.checkpoint = s.pos
	}
}
func (s *mp4Sink) finish() error {
	if s.limited || s.err != nil || s.remain != 0 || len(s.header) != 0 {
		if err := s.f.Truncate(s.checkpoint); err != nil {
			return err
		}
	}
	if s.checkpoint == 0 || s.fragments == 0 {
		return errors.New("E_MP4: no complete video fragment")
	}
	if s.err != nil && !errors.Is(s.err, ErrByteLimit) {
		return s.err
	}
	if !s.limited && (s.remain != 0 || len(s.header) != 0) {
		return errors.New("E_MP4: truncated encoder output")
	}
	return nil
}
