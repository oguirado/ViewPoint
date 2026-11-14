package x1

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// Unit test: 2x2 repack with padded strides and untouched destination padding
func TestRepack2x2WithPadding(t *testing.T) {
	w, h := 2, 2
	srcStride := 10 // >= 4*w (8)
	dstStride := 8  // >= 3*w (6)

	src := make([]byte, (h-1)*srcStride+4*w)   // 1*10 + 8 = 18
	dst := make([]byte, (h-1)*dstStride+3*w+2) // 1*8 + 6 + 2 extra tail bytes = 16

	// Fill dst with sentinel 0xAA
	for i := range dst {
		dst[i] = 0xAA
	}

	// Row 0, Pixel 0: B=0x11, G=0x22, R=0x33, X=0xFF
	src[0], src[1], src[2], src[3] = 0x11, 0x22, 0x33, 0xFF
	// Row 0, Pixel 1: B=0x44, G=0x55, R=0x66, X=0xFF
	src[4], src[5], src[6], src[7] = 0x44, 0x55, 0x66, 0xFF
	// Row 0 padding (bytes 8, 9): untouched/irrelevant

	// Row 1, Pixel 0: B=0x77, G=0x88, R=0x99, X=0xFF
	src[10], src[11], src[12], src[13] = 0x77, 0x88, 0x99, 0xFF
	// Row 1, Pixel 1: B=0xAA, G=0xBB, R=0xCC, X=0xFF
	src[14], src[15], src[16], src[17] = 0xAA, 0xBB, 0xCC, 0xFF

	err := RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Expected dst row 0: R,G,B for px0 (33,22,11), R,G,B for px1 (66,55,44), then padding (6,7) untouched (AA, AA)
	expectedDstRow0 := []byte{0x33, 0x22, 0x11, 0x66, 0x55, 0x44, 0xAA, 0xAA}
	if !bytes.Equal(dst[0:8], expectedDstRow0) {
		t.Errorf("dst row 0 mismatch: got %x, want %x", dst[0:8], expectedDstRow0)
	}

	// Expected dst row 1: R,G,B for px0 (99,88,77), R,G,B for px1 (CC,BB,AA), then padding (14,15) untouched (AA, AA)
	expectedDstRow1 := []byte{0x99, 0x88, 0x77, 0xCC, 0xBB, 0xAA, 0xAA, 0xAA}
	if !bytes.Equal(dst[8:16], expectedDstRow1) {
		t.Errorf("dst row 1 mismatch: got %x, want %x", dst[8:16], expectedDstRow1)
	}
}

// Unit test: Zero dimensions repack
func TestRepackZeroDim(t *testing.T) {
	dst := []byte{0x55, 0x55}
	err := RepackBGRXToRGB([]byte{}, 0, 10, 0, dst, 0)
	if err != nil {
		t.Fatalf("expected nil error for w=0, got %v", err)
	}
	if dst[0] != 0x55 || dst[1] != 0x55 {
		t.Fatalf("dst modified on w=0")
	}

	err = RepackBGRXToRGB([]byte{}, 10, 0, 40, dst, 30)
	if err != nil {
		t.Fatalf("expected nil error for h=0, got %v", err)
	}
}

// Unit test: Errors for both kernels
func TestErrorCodes(t *testing.T) {
	// Repack errors
	src := make([]byte, 100)
	dst := make([]byte, 100)

	if !errors.Is(RepackBGRXToRGB(src, -1, 10, 40, dst, 30), ErrDim) {
		t.Errorf("expected ErrDim for w=-1")
	}
	if !errors.Is(RepackBGRXToRGB(src, 16385, 10, 16385*4, dst, 16385*3), ErrDim) {
		t.Errorf("expected ErrDim for w=16385")
	}
	if !errors.Is(RepackBGRXToRGB(src, 10, -1, 40, dst, 30), ErrDim) {
		t.Errorf("expected ErrDim for h=-1")
	}
	if !errors.Is(RepackBGRXToRGB(src, 10, 16385, 40, dst, 30), ErrDim) {
		t.Errorf("expected ErrDim for h=16385")
	}

	if !errors.Is(RepackBGRXToRGB(src, 10, 10, 39, dst, 30), ErrStride) {
		t.Errorf("expected ErrStride for srcStride < 4*w")
	}
	if !errors.Is(RepackBGRXToRGB(src, 10, 10, 40, dst, 29), ErrStride) {
		t.Errorf("expected ErrStride for dstStride < 3*w")
	}

	shortSrc := make([]byte, (10-1)*40+40-1) // 1 byte short
	if !errors.Is(RepackBGRXToRGB(shortSrc, 10, 10, 40, dst, 30), ErrShort) {
		t.Errorf("expected ErrShort for short src buffer")
	}
	shortDst := make([]byte, (10-1)*30+30-1) // 1 byte short
	if !errors.Is(RepackBGRXToRGB(src, 10, 10, 40, shortDst, 30), ErrShort) {
		t.Errorf("expected ErrShort for short dst buffer")
	}

	// DamageTiles errors
	if _, _, err := DamageTiles(0, 10, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for w=0 in DamageTiles")
	}
	if _, _, err := DamageTiles(16385, 10, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for w=16385 in DamageTiles")
	}
	if _, _, err := DamageTiles(10, 0, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for h=0 in DamageTiles")
	}
	if _, _, err := DamageTiles(10, 16385, 32, nil); !errors.Is(err, ErrDim) {
		t.Errorf("expected ErrDim for h=16385 in DamageTiles")
	}

	if _, _, err := DamageTiles(100, 100, 0, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("expected ErrGrid for s=0")
	}
	if _, _, err := DamageTiles(100, 100, 3, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("expected ErrGrid for s=3 (not power of 2)")
	}
	if _, _, err := DamageTiles(100, 100, 512, nil); !errors.Is(err, ErrGrid) {
		t.Errorf("expected ErrGrid for s=512 (>256)")
	}

	invalidRects := []Rect{{X: 0, Y: 0, W: -1, H: 10}}
	if _, _, err := DamageTiles(100, 100, 32, invalidRects); !errors.Is(err, ErrRect) {
		t.Errorf("expected ErrRect for negative W")
	}
	invalidRectsH := []Rect{{X: 0, Y: 0, W: 10, H: -5}}
	if _, _, err := DamageTiles(100, 100, 32, invalidRectsH); !errors.Is(err, ErrRect) {
		t.Errorf("expected ErrRect for negative H")
	}
}

// Worked check from specification: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words;
// a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
func TestWorkedCheck(t *testing.T) {
	w, h, s := 1920, 1080, 32
	rects := []Rect{
		{X: 96, Y: 0, W: 192, H: 32},
	}

	words, popcount, err := DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(words) != 32 {
		t.Fatalf("expected 32 words, got %d", len(words))
	}

	// 192x32 at x=96, y=0:
	// x0 = 96 -> tx0 = 96/32 = 3
	// x1 = 96+192 = 288 -> tx1 = ceil(288/32) = 9
	// ty0 = 0, ty1 = 1
	// Tiles set: 3, 4, 5, 6, 7, 8 (total 6 tiles)
	if popcount != 6 {
		t.Errorf("expected popcount 6, got %d", popcount)
	}

	// All bits 3..8 in word 0 should be set
	expectedWord0 := uint64((1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8))
	if words[0] != expectedWord0 {
		t.Errorf("word 0 mismatch: got 0x%x, want 0x%x", words[0], expectedWord0)
	}

	for i := 1; i < len(words); i++ {
		if words[i] != 0 {
			t.Errorf("expected word %d to be 0, got 0x%x", i, words[i])
		}
	}
}

// Unit test: x=33 with w=0 setting no bit
func TestZeroWidthRect(t *testing.T) {
	rects := []Rect{
		{X: 33, Y: 10, W: 0, H: 20},
	}

	words, popcount, err := DamageTiles(100, 100, 32, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if popcount != 0 {
		t.Errorf("expected popcount 0 for w=0, got %d", popcount)
	}

	for i, word := range words {
		if word != 0 {
			t.Errorf("expected word %d to be 0, got 0x%x", i, word)
		}
	}
}

// Unit test: A rectangle crossing the partial bottom row
func TestPartialBottomRow(t *testing.T) {
	// W=100, H=100, s=32 -> gw=4, gh=4 (tiles: row 0..3, cols 0..3; total 16 tiles)
	// Partial bottom row: Y in 96..100 -> ty=3 (row 3 is 96..100)
	w, h, s := 100, 100, 32
	rects := []Rect{
		{X: 10, Y: 95, W: 50, H: 10}, // Y=95 to 105 clipped to 100 -> spans ty=2 (95/32=2) and ty=3 (100/32 ceil -> 4, so ty=2,3)
	}

	words, popcount, err := DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// X: 10 to 60 -> tx0 = 0, tx1 = ceil(60/32) = 2 (tx=0, 1)
	// Y: 95 to 100 (clipped) -> ty0 = 2, ty1 = 4 (ty=2, 3)
	// Tiles set: (ty=2, tx=0 -> t=2*4+0=8), (ty=2, tx=1 -> t=9), (ty=3, tx=0 -> t=12), (ty=3, tx=1 -> t=13)
	// Total 4 tiles
	if popcount != 4 {
		t.Errorf("expected popcount 4, got %d", popcount)
	}

	expectedWord0 := uint64((1 << 8) | (1 << 9) | (1 << 12) | (1 << 13))
	if words[0] != expectedWord0 {
		t.Errorf("word 0 mismatch: got 0x%x, want 0x%x", words[0], expectedWord0)
	}
}

// Unit test: SplitMix64 and FNV-1a 64 pinned values
func TestSplitMixAndFNV1aPinned(t *testing.T) {
	sm := NewSplitMix64(0)
	firstVal := sm.Next()
	expectedVal := uint64(0xe220a8397b1dcdaf)
	if firstVal != expectedVal {
		t.Errorf("SplitMix64 seed 0 step 1: got 0x%x, want 0x%x", firstVal, expectedVal)
	}

	fnvEmpty := FNV1a64([]byte{})
	const expectedEmpty uint64 = 0xcbf29ce484222325
	if fnvEmpty != expectedEmpty {
		t.Errorf("FNV1a empty mismatch: got 0x%x, want 0x%x", fnvEmpty, expectedEmpty)
	}

	fnvTest := FNV1a64([]byte("123456789"))
	const expectedTest uint64 = 0x6d5573923c6cdfc
	if fnvTest != expectedTest {
		t.Errorf("FNV1a test mismatch: got 0x%x, want 0x%x", fnvTest, expectedTest)
	}
}

// Fixture loader and runner
func TestFixtures(t *testing.T) {
	fixtureDir := filepath.Join("..", "fixtures")
	repackCasesPath := filepath.Join(fixtureDir, "repack_cases.json")

	if _, err := os.Stat(repackCasesPath); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	// If repack_cases.json exists, run repack and damage cases
	t.Run("RepackFixtures", func(t *testing.T) {
		type RepackCase struct {
			Name        string  `json:"name"`
			W           int     `json:"w"`
			H           int     `json:"h"`
			SrcStride   int     `json:"src_stride"`
			DstStride   int     `json:"dst_stride"`
			Seed        *uint64 `json:"seed,omitempty"`
			ExpectedFNV *string `json:"expected_fnv,omitempty"`
		}

		data, err := os.ReadFile(repackCasesPath)
		if err != nil {
			t.Fatalf("failed to read repack_cases.json: %v", err)
		}

		var cases []RepackCase
		if err := json.Unmarshal(data, &cases); err != nil {
			t.Fatalf("failed to unmarshal repack_cases.json: %v", err)
		}

		for _, tc := range cases {
			t.Run(tc.Name, func(t *testing.T) {
				if tc.Seed != nil {
					srcLen := (tc.H-1)*tc.SrcStride + 4*tc.W
					dstLen := (tc.H-1)*tc.DstStride + 3*tc.W
					src := make([]byte, srcLen)
					dst := make([]byte, dstLen)

					FillBufferSplitMix64(*tc.Seed, src)
					err := RepackBGRXToRGB(src, tc.W, tc.H, tc.SrcStride, dst, tc.DstStride)
					if err != nil {
						t.Fatalf("unexpected error: %v", err)
					}

					if tc.ExpectedFNV != nil {
						gotFNV := FNV1a64(dst)
						gotHex := fmt.Sprintf("%016x", gotFNV)
						if gotHex != *tc.ExpectedFNV {
							t.Errorf("FNV mismatch for %s: got %s, want %s", tc.Name, gotHex, *tc.ExpectedFNV)
						}
					}
				}
			})
		}
	})

	damageCasesPath := filepath.Join(fixtureDir, "damage_cases.json")
	t.Run("DamageFixtures", func(t *testing.T) {
		if _, err := os.Stat(damageCasesPath); os.IsNotExist(err) {
			t.Skip("damage fixtures absent")
		}
	})
}

// Benchmarks
func BenchmarkRepackBGRXToRGB(b *testing.B) {
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

func BenchmarkDamageTiles(b *testing.B) {
	w, h, s := 1920, 1080, 32
	rects := []Rect{
		{X: 100, Y: 100, W: 500, H: 400},
		{X: 300, Y: 200, W: 800, H: 600},
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, _, _ = DamageTiles(w, h, s, rects)
	}
}
