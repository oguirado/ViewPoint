package x1

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestRepack2x2WithPadding(t *testing.T) {
	w, h := 2, 2
	srcStride := 12 // 4*w = 8, padding = 4
	dstStride := 8  // 3*w = 6, padding = 2

	src := make([]byte, (h-1)*srcStride+4*w)
	// fill src pixel (x, y) with B, G, R, X
	// Row 0:
	// x=0: B=10, G=11, R=12, X=99
	src[0], src[1], src[2], src[3] = 10, 11, 12, 99
	// x=1: B=20, G=21, R=22, X=99
	src[4], src[5], src[6], src[7] = 20, 21, 22, 99

	// Row 1 (starts at srcStride = 12):
	// x=0: B=30, G=31, R=32, X=99
	src[12], src[13], src[14], src[15] = 30, 31, 32, 99
	// x=1: B=40, G=41, R=42, X=99
	src[16], src[17], src[18], src[19] = 40, 41, 42, 99

	dstLen := (h-1)*dstStride + 3*w + 2 // extra destination buffer with trailing padding
	dst := make([]byte, dstLen)
	for i := range dst {
		dst[i] = 0xEE // sentinel
	}

	err := RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Row 0 expected RGB:
	// x=0: R=12, G=11, B=10
	// x=1: R=22, G=21, B=20
	// Row 0 padding at indices 6, 7 must be untouched (0xEE)
	expectedDstRow0 := []byte{12, 11, 10, 22, 21, 20, 0xEE, 0xEE}
	for i := 0; i < 8; i++ {
		if dst[i] != expectedDstRow0[i] {
			t.Errorf("dst[%d] = %d, want %d", i, dst[i], expectedDstRow0[i])
		}
	}

	// Row 1 expected RGB (starts at dstStride = 8):
	// x=0: R=32, G=31, B=30
	// x=1: R=42, G=41, B=40
	// Row 1 padding at indices 14, 15 must be untouched (0xEE)
	expectedDstRow1 := []byte{32, 31, 30, 42, 41, 40, 0xEE, 0xEE}
	for i := 0; i < 8; i++ {
		if dst[8+i] != expectedDstRow1[i] {
			t.Errorf("dst[%d] = %d, want %d", 8+i, dst[8+i], expectedDstRow1[i])
		}
	}
}

func TestErrorCodes(t *testing.T) {
	// ErrDim
	if err := RepackBGRXToRGB(make([]byte, 100), -1, 10, 32, make([]byte, 100), 24); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for w=-1, got %v", err)
	}
	if err := RepackBGRXToRGB(make([]byte, 100), 16385, 10, 16385*4, make([]byte, 100), 16385*3); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for w=16385, got %v", err)
	}
	if _, _, err := DamageTiles(0, 10, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for w=0 in DamageTiles, got %v", err)
	}
	if _, _, err := DamageTiles(10, 16385, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for h=16385 in DamageTiles, got %v", err)
	}

	// ErrStride
	if err := RepackBGRXToRGB(make([]byte, 100), 10, 10, 39, make([]byte, 100), 30); !errors.Is(err, ErrStride) {
		t.Errorf("expected ErrStride for srcStride < 4W, got %v", err)
	}
	if err := RepackBGRXToRGB(make([]byte, 100), 10, 10, 40, make([]byte, 100), 29); !errors.Is(err, ErrStride) {
		t.Errorf("expected ErrStride for dstStride < 3W, got %v", err)
	}

	// ErrShort
	if err := RepackBGRXToRGB(make([]byte, 39), 10, 1, 40, make([]byte, 30), 30); !errors.Is(err, ErrShort) {
		t.Errorf("expected ErrShort for short src, got %v", err)
	}
	if err := RepackBGRXToRGB(make([]byte, 40), 10, 1, 40, make([]byte, 29), 30); !errors.Is(err, ErrShort) {
		t.Errorf("expected ErrShort for short dst, got %v", err)
	}

	// ErrGrid
	if _, _, err := DamageTiles(10, 10, 0, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("expected ErrGrid for s=0, got %v", err)
	}
	if _, _, err := DamageTiles(10, 10, 3, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("expected ErrGrid for s=3, got %v", err)
	}
	if _, _, err := DamageTiles(10, 10, 512, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("expected ErrGrid for s=512, got %v", err)
	}

	// ErrRect
	rects := []Rect{{X: 0, Y: 0, W: -1, H: 10}}
	if _, _, err := DamageTiles(10, 10, 16, rects); !errors.Is(err, ErrRect) {
		t.Errorf("expected ErrRect for negative rect W, got %v", err)
	}
	rects2 := []Rect{{X: 0, Y: 0, W: 10, H: -5}}
	if _, _, err := DamageTiles(10, 10, 16, rects2); !errors.Is(err, ErrRect) {
		t.Errorf("expected ErrRect for negative rect H, got %v", err)
	}
}

func TestWorkedCheck(t *testing.T) {
	// Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
	// a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
	w, h, s := 1920, 1080, 32
	rects := []Rect{{X: 96, Y: 0, W: 192, H: 32}}

	words, popcount, err := DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(words) != 32 {
		t.Fatalf("expected 32 words, got %d", len(words))
	}

	if popcount != 6 {
		t.Fatalf("expected popcount 6, got %d", popcount)
	}

	// Tiles 3 to 8 inclusive means bits 3, 4, 5, 6, 7, 8 set.
	// 1<<3 | 1<<4 | 1<<5 | 1<<6 | 1<<7 | 1<<8 = 8 + 16 + 32 + 64 + 128 + 256 = 504
	expectedWord0 := uint64((1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8))
	if words[0] != expectedWord0 {
		t.Fatalf("word 0 = %d, want %d", words[0], expectedWord0)
	}

	for i := 1; i < len(words); i++ {
		if words[i] != 0 {
			t.Fatalf("word %d = %d, want 0", i, words[i])
		}
	}
}

func TestZeroWidthRectSkipped(t *testing.T) {
	// x 33, w 0 sets nothing
	w, h, s := 100, 100, 16
	rects := []Rect{{X: 33, Y: 10, W: 0, H: 20}}

	words, popcount, err := DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if popcount != 0 {
		t.Fatalf("expected popcount 0, got %d", popcount)
	}

	for i, word := range words {
		if word != 0 {
			t.Fatalf("word[%d] = %d, want 0", i, word)
		}
	}
}

func TestPartialBottomRowRect(t *testing.T) {
	// W 100, H 35, s 16. gw = 7, gh = 3 (tiles at y: 0..15, 16..31, 32..34 (partial)).
	// Rect from Y=30, H=10 => clipped y0=30, y1=35.
	// Covers ty=1 (30/16) and ty=2 (ceil(35/16) = 3 -> ty=2).
	// X=0, W=16 => tx=0.
	// Tiles set: ty=1, tx=0 (t = 1*7 + 0 = 7) and ty=2, tx=0 (t = 2*7 + 0 = 14).
	w, h, s := 100, 35, 16
	rects := []Rect{{X: 0, Y: 30, W: 16, H: 10}}

	words, popcount, err := DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if popcount != 2 {
		t.Fatalf("expected popcount 2, got %d", popcount)
	}

	expectedWord := uint64((1 << 7) | (1 << 14))
	if words[0] != expectedWord {
		t.Fatalf("words[0] = %d, want %d", words[0], expectedWord)
	}
}

func TestSplitMix64AndFNV1a64Pinned(t *testing.T) {
	sm := NewSplitMix64(0x123456789ABCDEF0)
	v1 := sm.Next()
	v2 := sm.Next()

	// Verify SplitMix64 determinism against algorithm output
	expectedV1 := uint64(0x161922c645ce50e8)
	expectedV2 := uint64(0xad760cafa1697b60)

	if v1 != expectedV1 {
		t.Errorf("SplitMix64 v1 = 0x%x, want 0x%x", v1, expectedV1)
	}
	if v2 != expectedV2 {
		t.Errorf("SplitMix64 v2 = 0x%x, want 0x%x", v2, expectedV2)
	}

	buf := []byte("hello world")
	hash := FNV1a64(buf)
	expectedHash := uint64(0x779a65e7023cd2e7)
	if hash != expectedHash {
		t.Errorf("FNV1a64 = 0x%x, want 0x%x", hash, expectedHash)
	}
}

type RepackCase struct {
	Name      string `json:"name"`
	W         int    `json:"w"`
	H         int    `json:"h"`
	SrcStride int    `json:"src_stride"`
	DstStride int    `json:"dst_stride"`
	SrcOffset int    `json:"src_offset"`
	DstOffset int    `json:"dst_offset"`
	Seed      uint64 `json:"seed,omitempty"`
	OutputFNV uint64 `json:"output_fnv,omitempty"`
	Error     string `json:"error,omitempty"`
}

type DamageCase struct {
	Name     string   `json:"name"`
	W        int      `json:"w"`
	H        int      `json:"h"`
	S        int      `json:"s"`
	Rects    []Rect   `json:"rects"`
	Words    []string `json:"words,omitempty"`
	Popcount int      `json:"popcount,omitempty"`
	Error    string   `json:"error,omitempty"`
}

func TestFixtures(t *testing.T) {
	fixtureDir := filepath.Join("..", "fixtures")
	if _, err := os.Stat(fixtureDir); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	repackCasesPath := filepath.Join(fixtureDir, "repack_cases.json")
	damageCasesPath := filepath.Join(fixtureDir, "damage_cases.json")

	if _, err := os.Stat(repackCasesPath); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	if _, err := os.Stat(damageCasesPath); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	// Test Repack Cases
	repackData, err := os.ReadFile(repackCasesPath)
	if err != nil {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	var repackCases []RepackCase
	if err := json.Unmarshal(repackData, &repackCases); err != nil {
		t.Fatalf("failed to parse repack_cases.json: %v", err)
	}

	// Load binary blobs if present
	srcBin, _ := os.ReadFile(filepath.Join(fixtureDir, "repack_src.bin"))
	dstBin, _ := os.ReadFile(filepath.Join(fixtureDir, "repack_dst.bin"))

	for _, tc := range repackCases {
		t.Run("repack/"+tc.Name, func(t *testing.T) {
			var src, dst []byte
			if tc.Seed != 0 {
				reqSrcLen := 0
				if tc.W > 0 && tc.H > 0 {
					reqSrcLen = (tc.H-1)*tc.SrcStride + 4*tc.W
				}
				reqDstLen := 0
				if tc.W > 0 && tc.H > 0 {
					reqDstLen = (tc.H-1)*tc.DstStride + 3*tc.W
				}
				src = make([]byte, reqSrcLen+tc.SrcOffset)
				dst = make([]byte, reqDstLen+tc.DstOffset)
				sm := NewSplitMix64(tc.Seed)
				sm.FillBytes(src)
			} else {
				reqSrcLen := 0
				if tc.W > 0 && tc.H > 0 {
					reqSrcLen = (tc.H-1)*tc.SrcStride + 4*tc.W
				}
				reqDstLen := 0
				if tc.W > 0 && tc.H > 0 {
					reqDstLen = (tc.H-1)*tc.DstStride + 3*tc.W
				}

				if tc.SrcOffset+reqSrcLen <= len(srcBin) {
					src = srcBin[tc.SrcOffset : tc.SrcOffset+reqSrcLen]
				} else {
					src = make([]byte, reqSrcLen+tc.SrcOffset)
				}

				if tc.DstOffset+reqDstLen <= len(dstBin) {
					dst = make([]byte, reqDstLen+tc.DstOffset)
					copy(dst, dstBin[tc.DstOffset:])
				} else {
					dst = make([]byte, reqDstLen+tc.DstOffset)
				}
			}

			srcSlice := src[tc.SrcOffset:]
			dstSlice := dst[tc.DstOffset:]

			err := RepackBGRXToRGB(srcSlice, tc.W, tc.H, tc.SrcStride, dstSlice, tc.DstStride)

			if tc.Error != "" {
				if err == nil {
					t.Fatalf("expected error %s, got nil", tc.Error)
				}
				switch tc.Error {
				case "E_DIM":
					if !errors.Is(err, ErrDim) {
						t.Fatalf("expected ErrDim, got %v", err)
					}
				case "E_STRIDE":
					if !errors.Is(err, ErrStride) {
						t.Fatalf("expected ErrStride, got %v", err)
					}
				case "E_SHORT":
					if !errors.Is(err, ErrShort) {
						t.Fatalf("expected ErrShort, got %v", err)
					}
				}
			} else {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if tc.OutputFNV != 0 {
					hash := FNV1a64(dstSlice)
					if hash != tc.OutputFNV {
						t.Fatalf("output FNV1a mismatch: got 0x%x, want 0x%x", hash, tc.OutputFNV)
					}
				}
			}
		})
	}

	// Test Damage Cases
	damageData, err := os.ReadFile(damageCasesPath)
	if err != nil {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	var damageCases []DamageCase
	if err := json.Unmarshal(damageData, &damageCases); err != nil {
		t.Fatalf("failed to parse damage_cases.json: %v", err)
	}

	for _, tc := range damageCases {
		t.Run("damage/"+tc.Name, func(t *testing.T) {
			words, popcount, err := DamageTiles(tc.W, tc.H, tc.S, tc.Rects)
			if tc.Error != "" {
				if err == nil {
					t.Fatalf("expected error %s, got nil", tc.Error)
				}
				switch tc.Error {
				case "E_DIM":
					if !errors.Is(err, ErrDim) {
						t.Fatalf("expected ErrDim, got %v", err)
					}
				case "E_GRID":
					if !errors.Is(err, ErrGrid) {
						t.Fatalf("expected ErrGrid, got %v", err)
					}
				case "E_RECT":
					if !errors.Is(err, ErrRect) {
						t.Fatalf("expected ErrRect, got %v", err)
					}
				}
			} else {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if popcount != tc.Popcount {
					t.Fatalf("popcount mismatch: got %d, want %d", popcount, tc.Popcount)
				}
				if len(words) != len(tc.Words) {
					t.Fatalf("words length mismatch: got %d, want %d", len(words), len(tc.Words))
				}
				for i, wStr := range tc.Words {
					actualStr := fmt.Sprintf("%016x", words[i])
					if actualStr != wStr {
						t.Fatalf("word %d mismatch: got %s, want %s", i, actualStr, wStr)
					}
				}
			}
		})
	}
}

func BenchmarkRepackBGRXToRGB(b *testing.B) {
	w, h := 1920, 1080
	srcStride := w * 4
	dstStride := w * 3
	src := make([]byte, h*srcStride)
	dst := make([]byte, h*dstStride)

	b.SetBytes(int64(w * h * 4))
	b.ResetTimer()

	for i := 0; i < b.N; i++ {
		_ = RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	}
}

// Hex decoding helper if needed
func parseHexWords(hexStrs []string) ([]uint64, error) {
	res := make([]uint64, len(hexStrs))
	for i, s := range hexStrs {
		b, err := hex.DecodeString(s)
		if err != nil || len(b) != 8 {
			return nil, fmt.Errorf("invalid hex string: %s", s)
		}
		var val uint64
		for j := 0; j < 8; j++ {
			val |= uint64(b[j]) << (j * 8)
		}
		res[i] = val
	}
	return res, nil
}
