package ramcap

import "bytes"

// ConsistentRead is used at the framebuffer reader, never as a host exec retry.
// A private copy prevents an mmap-backed callback from changing the previous read.
func ConsistentRead(read func() ([]byte, error), tries int) ([]byte, error) {
	if read == nil || tries < 2 || tries > 8 {
		return nil, fail("E_TORN")
	}
	prev, e := read()
	if e != nil {
		return nil, e
	}
	prev = bytes.Clone(prev)
	for i := 1; i < tries; i++ {
		cur, e := read()
		if e != nil {
			return nil, e
		}
		if bytes.Equal(cur, prev) {
			return bytes.Clone(cur), nil
		}
		prev = bytes.Clone(cur)
	}
	return nil, fail("E_TORN")
}
