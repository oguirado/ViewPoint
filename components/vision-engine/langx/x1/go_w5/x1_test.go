package x1

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func splitMix64Buf(seed uint64, length int) []byte {
	buf := make([]byte, length)
	state := seed
	off := 0
	for off < length {
		state += 0x9E3779B97F4A7C15
		z := state
		z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
		z = (z ^ (z >> 27)) * 0x94D049BB133111EB
		z = z ^ (z >> 31)

		var tmp [8]byte
		binary.LittleEndian.PutUint64(tmp[:], z)
		n := copy(buf[off:], tmp[:])
		off += n
	}
	return buf
}

func fnv1a64(data []byte) uint64 {
	const offsetBasis uint64 = 0xcbf29ce484222325
	const prime uint64 = 0x100000001b3
	h := offsetBasis
	for _, b := range data {
		h ^= uint64(b)
		h *= prime
	}
	return h
}

func TestSplitMixAndFNV1a(t *testing.T) {
	buf := splitMix64Buf(0, 16)
	expectedBufHex := "afcd1d7b39a820e2f465b9a16a9e786e"
	actualBufHex := fmt.Sprintf("%x", buf)
	if actualBufHex != expectedBufHex {
		t.Fatalf("SplitMix64 output mismatch: got %s, want %s", actualBufHex, expectedBufHex)
	}

	h := fnv1a64(buf)
	expectedFNV := uint64(0x1615cf11570845b5)
	if h != expectedFNV {
		t.Fatalf("FNV-1a 64 mismatch: got 0x%x, want 0x%x", h, expectedFNV)
	}
}

func TestUnitRepack2x2(t *testing.T) {
	w, h := 2, 2
	srcStride := 12
	dstStride := 10

	src := make([]byte, (h-1)*srcStride+4*w)
	// Row 0, pixel 0: B=10, G=11, R=12, X=99
	src[0], src[1], src[2], src[3] = 10, 11, 12, 99
	// Row 0, pixel 1: B=20, G=21, R=22, X=99
	src[4], src[5], src[6], src[7] = 20, 21, 22, 99

	// Row 1, pixel 0: B=30, G=31, R=32, X=99
	src[12+0], src[12+1], src[12+2], src[12+3] = 30, 31, 32, 99
	// Row 1, pixel 1: B=40, G=41, R=42, X=99
	src[12+4], src[12+5], src[12+6], src[12+7] = 40, 41, 42, 99

	dst := make([]byte, h*dstStride)
	for i := range dst {
		dst[i] = 0xFF
	}

	err := RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	if err != nil {
		t.Fatalf("unexpected repack error: %v", err)
	}

	// Verify row 0: R, G, B for p0, p1
	expectedR0 := []byte{12, 11, 10, 22, 21, 20}
	for i, b := range expectedR0 {
		if dst[i] != b {
			t.Errorf("row 0 dst[%d] = %d, want %d", i, dst[i], b)
		}
	}
	// Check row 0 padding untouched
	for i := 6; i < dstStride; i++ {
		if dst[i] != 0xFF {
			t.Errorf("dst padding row 0 byte %d touched: %d", i, dst[i])
		}
	}

	// Verify row 1: R, G, B for p0, p1
	expectedR1 := []byte{32, 31, 30, 42, 41, 40}
	for i, b := range expectedR1 {
		if dst[dstStride+i] != b {
			t.Errorf("row 1 dst[%d] = %d, want %d", i, dst[dstStride+i], b)
		}
	}
	// Check row 1 padding untouched
	for i := dstStride + 6; i < 2*dstStride; i++ {
		if dst[i] != 0xFF {
			t.Errorf("dst padding row 1 byte %d touched: %d", i, dst[i])
		}
	}
}

func TestErrorSentinels(t *testing.T) {
	// ErrDim in Repack
	if err := RepackBGRXToRGB(nil, -1, 10, 40, nil, 30); !errors.Is(err, ErrDim) {
		t.Errorf("want ErrDim for w=-1, got %v", err)
	}
	if err := RepackBGRXToRGB(nil, 16385, 10, 40, nil, 30); !errors.Is(err, ErrDim) {
		t.Errorf("want ErrDim for w=16385, got %v", err)
	}

	// ErrStride
	if err := RepackBGRXToRGB(nil, 10, 10, 39, nil, 30); !errors.Is(err, ErrStride) {
		t.Errorf("want ErrStride for srcStride < 4W, got %v", err)
	}
	if err := RepackBGRXToRGB(nil, 10, 10, 40, nil, 29); !errors.Is(err, ErrStride) {
		t.Errorf("want ErrStride for dstStride < 3W, got %v", err)
	}

	// ErrShort
	srcShort := make([]byte, 10)
	dstShort := make([]byte, 100)
	if err := RepackBGRXToRGB(srcShort, 2, 2, 8, dstShort, 6); !errors.Is(err, ErrShort) {
		t.Errorf("want ErrShort for src short, got %v", err)
	}

	// ErrDim in Damage
	if _, _, err := DamageTiles(0, 1080, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("want ErrDim for w=0 in DamageTiles, got %v", err)
	}

	// ErrGrid in Damage
	if _, _, err := DamageTiles(1920, 1080, 33, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("want ErrGrid for s=33 in DamageTiles, got %v", err)
	}

	// ErrRect in Damage
	rectsBad := []Rect{{X: 0, Y: 0, W: -1, H: 10}}
	if _, _, err := DamageTiles(1920, 1080, 32, rectsBad); !errors.Is(err, ErrRect) {
		t.Errorf("want ErrRect for negative W in DamageTiles, got %v", err)
	}
}

func TestWorkedCheck(t *testing.T) {
	// Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
	// a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
	rects := []Rect{
		{X: 96, Y: 0, W: 192, H: 32},
	}
	words, pop, err := DamageTiles(1920, 1080, 32, rects)
	if err != nil {
		t.Fatalf("unexpected DamageTiles error: %v", err)
	}
	if len(words) != 32 {
		t.Fatalf("expected 32 words, got %d", len(words))
	}
	if pop != 6 {
		t.Fatalf("expected popcount 6, got %d", pop)
	}

	// Tiles 3..8 are bits 3, 4, 5, 6, 7, 8 -> 0b111111000 = 0x1F8 in word 0
	expectedWord0 := uint64(0x1F8)
	if words[0] != expectedWord0 {
		t.Errorf("word 0 = 0x%x, want 0x%x", words[0], expectedWord0)
	}
	for i := 1; i < len(words); i++ {
		if words[i] != 0 {
			t.Errorf("word %d = 0x%x, want 0", i, words[i])
		}
	}
}

func TestDamageZeroWidthRect(t *testing.T) {
	// x 33 with w 0 sets nothing
	rects := []Rect{
		{X: 33, Y: 0, W: 0, H: 32},
	}
	words, pop, err := DamageTiles(1920, 1080, 32, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if pop != 0 {
		t.Errorf("expected 0 bits set for W=0 rect, got pop=%d", pop)
	}
	for i, wVal := range words {
		if wVal != 0 {
			t.Errorf("word %d = 0x%x, want 0", i, wVal)
		}
	}
}

func TestDamagePartialBottomRow(t *testing.T) {
	// W 100, H 100, s 32 -> gw 4, gh 4 (tiles 0..15).
	// Row 3 (ty=3) is y=96..100 (partial row of height 4).
	// Rect at X:0, Y:98, W:32, H:10 crosses partial bottom row.
	// Clipped y0=98, y1=100. tyMin=3, tyMax=4. txMin=0, txMax=1.
	// Tile t = 3*4 + 0 = 12.
	rects := []Rect{
		{X: 0, Y: 98, W: 32, H: 10},
	}
	words, pop, err := DamageTiles(100, 100, 32, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if pop != 1 {
		t.Errorf("expected pop 1, got %d", pop)
	}
	if words[0] != (uint64(1) << 12) {
		t.Errorf("expected tile 12 set, word 0 = 0x%x", words[0])
	}
}

type repackCase struct {
	Name      string `json:"name"`
	W         int    `json:"w"`
	H         int    `json:"h"`
	SrcStride int    `json:"src_stride"`
	DstStride int    `json:"dst_stride"`
	Seed      uint64 `json:"seed"`
	Offset    int    `json:"offset"`
	Length    int    `json:"length"`
	DstOffset int    `json:"dst_offset"`
	DstLength int    `json:"dst_length"`
	Expected  string `json:"expected"` // hex of output or FNV-1a hex
	Err       string `json:"err,omitempty"`
}

type damageCase struct {
	Name     string   `json:"name"`
	W        int      `json:"w"`
	H        int      `json:"h"`
	S        int      `json:"s"`
	Rects    []Rect   `json:"rects"`
	Expected []string `json:"expected"` // 16-digit lowercase hex
	Popcount int      `json:"popcount"`
	Err      string   `json:"err,omitempty"`
}

func TestFixtures(t *testing.T) {
	fixtureDir := filepath.Join("..", "fixtures")
	repackJsonPath := filepath.Join(fixtureDir, "repack_cases.json")
	damageJsonPath := filepath.Join(fixtureDir, "damage_cases.json")

	if _, err := os.Stat(repackJsonPath); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	repackData, err := os.ReadFile(repackJsonPath)
	if err != nil {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	var repackCases []repackCase
	if err := json.Unmarshal(repackData, &repackCases); err != nil {
		t.Fatalf("failed to unmarshal repack cases: %v", err)
	}

	var srcBin, dstBin []byte
	srcBinPath := filepath.Join(fixtureDir, "repack_src.bin")
	dstBinPath := filepath.Join(fixtureDir, "repack_dst.bin")

	srcBin, _ = os.ReadFile(srcBinPath)
	dstBin, _ = os.ReadFile(dstBinPath)

	for _, tc := range repackCases {
		t.Run("repack/"+tc.Name, func(t *testing.T) {
			var src, dst []byte
			if tc.Seed != 0 || len(srcBin) == 0 {
				srcLen := tc.Length
				if srcLen == 0 && tc.H > 0 {
					srcLen = (tc.H-1)*tc.SrcStride + 4*tc.W
				}
				src = splitMix64Buf(tc.Seed, srcLen)
			} else {
				src = srcBin[tc.Offset : tc.Offset+tc.Length]
			}

			dstLen := tc.DstLength
			if dstLen == 0 && tc.H > 0 {
				dstLen = tc.H * tc.DstStride
			}
			if len(dstBin) > 0 && tc.DstOffset < len(dstBin) {
				dst = make([]byte, dstLen)
				copy(dst, dstBin[tc.DstOffset:])
			} else {
				dst = make([]byte, dstLen)
			}

			err := RepackBGRXToRGB(src, tc.W, tc.H, tc.SrcStride, dst, tc.DstStride)
			if tc.Err != "" {
				if err == nil || err.Error() != tc.Err {
					t.Fatalf("expected error %s, got %v", tc.Err, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			hVal := fnv1a64(dst)
			actualHex := fmt.Sprintf("%016x", hVal)
			if tc.Expected != "" && actualHex != tc.Expected && fmt.Sprintf("%x", dst) != tc.Expected {
				t.Fatalf("repack output mismatch: got %s, want %s", actualHex, tc.Expected)
			}
		})
	}

	damageData, err := os.ReadFile(damageJsonPath)
	if err == nil {
		var damageCases []damageCase
		if err := json.Unmarshal(damageData, &damageCases); err == nil {
			for _, tc := range damageCases {
				t.Run("damage/"+tc.Name, func(t *testing.T) {
					words, pop, err := DamageTiles(tc.W, tc.H, tc.S, tc.Rects)
					if tc.Err != "" {
						if err == nil || err.Error() != tc.Err {
							t.Fatalf("expected error %s, got %v", tc.Err, err)
						}
						return
					}
					if err != nil {
						t.Fatalf("unexpected error: %v", err)
					}
					if pop != tc.Popcount {
						t.Fatalf("popcount mismatch: got %d, want %d", pop, tc.Popcount)
					}
					if len(words) != len(tc.Expected) {
						t.Fatalf("words len mismatch: got %d, want %d", len(words), len(tc.Expected))
					}
					for i, wVal := range words {
						hStr := fmt.Sprintf("%016x", wVal)
						if hStr != tc.Expected[i] {
							t.Fatalf("word %d mismatch: got %s, want %s", i, hStr, tc.Expected[i])
						}
					}
				})
			}
		}
	}
}

func BenchmarkRepack(b *testing.B) {
	w, h := 1920, 1080
	srcStride := w * 4
	dstStride := w * 3
	src := make([]byte, h*srcStride)
	dst := make([]byte, h*dstStride)

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	}
}
