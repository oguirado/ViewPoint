package capture

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"hash"
	"os"
	"path/filepath"
)

// FileMode is the mode of every frame/raw file the ring writes: owner rw, group r, no world bits (AW3-07).
const FileMode os.FileMode = 0o640

// RingSlots is the number of frames kept in frames.raw.
const RingSlots = 4

// FrameMeta describes one frame resident in the ring.
type FrameMeta struct {
	I           int    `json:"i"`
	TUS         uint64 `json:"t_us"`
	RenderIndex uint32 `json:"render_index"`
	SHA256      string `json:"sha256"`
	FileSHA256  string `json:"file_sha256"`
	RawOffset   int64  `json:"raw_offset"`
}

// Ring is a 4-slot raw RGB24 ring file inside a ramws run directory.
type Ring struct {
	Dir      string
	W, H     int
	f        *os.File
	frames   []FrameMeta
	total    int
	nextRI   uint32
	det      hash.Hash
	hdrWrote bool
}

// NewRing prepares a ring over dir (a path the caller took from the ramws shim); the file is created on first write.
func NewRing(dir string, w, h int) *Ring {
	return &Ring{Dir: dir, W: w, H: h, det: sha256.New()}
}

func (r *Ring) frameBytes() int { return r.W * r.H * 3 }

// SlotBytes is the on-RAM size of frames.raw.
func (r *Ring) SlotBytes() int64 { return int64(RingSlots * r.frameBytes()) }

func (r *Ring) open() error {
	if r.f != nil {
		return nil
	}
	// The run directory belongs to the ramws session (owner.json + .lock); never create it here.
	if st, err := os.Stat(r.Dir); err != nil || !st.IsDir() {
		return ioErr("ring_dir_missing", err)
	}
	// 0o640 set explicitly (umask-independent): group bits r-- keep a shared run dir's ACL mask readable (ADC2-W3-02).
	f, err := os.OpenFile(filepath.Join(r.Dir, "frames.raw"), os.O_RDWR|os.O_CREATE|os.O_TRUNC, FileMode)
	if err != nil {
		return ioErr("ring_open", err)
	}
	if err := f.Chmod(FileMode); err != nil {
		f.Close()
		return ioErr("ring_chmod", err)
	}
	if err := f.Truncate(r.SlotBytes()); err != nil {
		f.Close()
		return ioErr("ring_truncate", err)
	}
	r.f = f
	return nil
}

// Put stores one RGB24 frame, hashes it, advances render_index and returns its metadata.
func (r *Ring) Put(rgb []byte, tUS uint64) (FrameMeta, error) {
	if len(rgb) != r.frameBytes() {
		return FrameMeta{}, schemaErr("frame_size", nil)
	}
	if err := r.open(); err != nil {
		return FrameMeta{}, err
	}
	ri := r.nextRI
	off := int64(int(ri)%RingSlots) * int64(r.frameBytes())
	if _, err := r.f.WriteAt(rgb, off); err != nil {
		return FrameMeta{}, ioErr("ring_write", err)
	}
	sum := sha256.Sum256(rgb)
	hx := hex.EncodeToString(sum[:])
	m := FrameMeta{TUS: tUS, RenderIndex: ri, SHA256: hx, FileSHA256: hx, RawOffset: off}
	var dim [8]byte
	binary.BigEndian.PutUint32(dim[0:], uint32(r.W))
	binary.BigEndian.PutUint32(dim[4:], uint32(r.H))
	r.det.Write(dim[:])
	r.det.Write(rgb)
	r.frames = append(r.frames, m)
	if len(r.frames) > RingSlots {
		r.frames = r.frames[len(r.frames)-RingSlots:]
	}
	for i := range r.frames {
		r.frames[i].I = i
	}
	r.total++
	r.nextRI++
	return r.frames[len(r.frames)-1], nil
}

// Bytes returns the RGB24 bytes of the k-th resident frame (0 = oldest).
func (r *Ring) Bytes(k int) []byte {
	b := make([]byte, r.frameBytes())
	if k < 0 || k >= len(r.frames) || r.f == nil {
		return nil
	}
	if _, err := r.f.ReadAt(b, r.frames[k].RawOffset); err != nil {
		return nil
	}
	return b
}

// DetSHA256 is sha256 over (w,h,RGB24) of every captured frame: no path, run id or time (STORAGE LAW L10).
func (r *Ring) DetSHA256() string { return hex.EncodeToString(r.det.Sum(nil)) }

// Total is the number of frames captured since NewRing.
func (r *Ring) Total() int { return r.total }

// Close closes the ring file; removal of the run directory belongs to the ramws session owner.
func (r *Ring) Close() error {
	if r.f == nil {
		return nil
	}
	err := r.f.Close()
	r.f = nil
	return err
}
