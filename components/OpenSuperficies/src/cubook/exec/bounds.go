package exec

// Coordinate bounds (os-cu-0 W4 lane M, ADC3-W3-03, AW4-07). A point or rect is valid against the MEASURED screen (w, h from
// the wlcap START header), never just against the XTEST int16 ceiling. All comparisons are in int64 and no sum x+w is formed
// from untrusted operands (SW-0009 I3: `l <= M - o`), so an extreme operand cannot wrap into an accepted value (SW-0198 INV-2,
// VG-0060 §4.2 saturation idea).

const maxScreenDim = 32767 // XTEST coordinates are int16

func dimsOK(w, h int) bool { return w > 0 && h > 0 && w <= maxScreenDim && h <= maxScreenDim }

// InScreen reports 0 <= x < w and 0 <= y < h for p = [x, y] (exactly two coordinates).
func InScreen(p []int, w, h int) bool {
	if len(p) != 2 || !dimsOK(w, h) {
		return false
	}
	x, y := int64(p[0]), int64(p[1])
	return x >= 0 && y >= 0 && x < int64(w) && y < int64(h)
}

// InRect reports that r = [x, y, rw, rh] is a non-empty rect inside the w x h screen: x+rw <= w and y+rh <= h.
func InRect(r []int, w, h int) bool {
	if len(r) != 4 || !dimsOK(w, h) {
		return false
	}
	x, y, rw, rh := int64(r[0]), int64(r[1]), int64(r[2]), int64(r[3])
	return x >= 0 && y >= 0 && rw > 0 && rh > 0 && x < int64(w) && y < int64(h) && rw <= int64(w)-x && rh <= int64(h)-y
}
