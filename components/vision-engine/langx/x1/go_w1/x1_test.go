package x1_test

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"langx/x1/go_w1"
)

type RepackCase struct {
	Name      string `json:"name"`
	W         int    `json:"w"`
	H         int    `json:"h"`
	SrcStride int    `json:"src_stride"`
	DstStride int    `json:"dst_stride"`
	SrcOffset int    `json:"src_offset,omitempty"`
	DstOffset int    `json:"dst_offset,omitempty"`
	SrcLen    int    `json:"src_len,omitempty"`
	DstLen    int    `json:"dst_len,omitempty"`
	Err       string `json:"err,omitempty"`
	Seed      uint64 `json:"seed,omitempty"`
	DstFNV    uint64 `json:"dst_fnv,omitempty"`
}

type DamageCase struct {
	Name     string    `json:"name"`
	W        int       `json:"w"`
	H        int       `json:"h"`
	S        int       `json:"s"`
	Rects    []x1.Rect `json:"rects"`
	Err      string    `json:"err,omitempty"`
	WordsHex []string  `json:"words_hex,omitempty"`
	Popcount int       `json:"popcount,omitempty"`
}

func parseErr(e string) error {
	switch e {
	case "E_DIM":
		return x1.ErrDim
	case "E_STRIDE":
		return x1.ErrStride
	case "E_SHORT":
		return x1.ErrShort
	case "E_RECT":
		return x1.ErrRect
	case "E_GRID":
		return x1.ErrGrid
	default:
		return nil
	}
}

func TestFixtures(t *testing.T) {
	fixtureDir := filepath.Join("..", "fixtures")
	if _, err := os.Stat(fixtureDir); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	repackCasesFile := filepath.Join(fixtureDir, "repack_cases.json")
	if _, err := os.Stat(repackCasesFile); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	repackBytes, err := os.ReadFile(repackCasesFile)
	if err != nil {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	var repackCases []RepackCase
	if err := json.Unmarshal(repackBytes, &repackCases); err != nil {
		t.Fatalf("failed to unmarshal repack_cases.json: %v", err)
	}

	var srcBin, dstBin []byte
	srcBinPath := filepath.Join(fixtureDir, "repack_src.bin")
	dstBinPath := filepath.Join(fixtureDir, "repack_dst.bin")
	if b, err := os.ReadFile(srcBinPath); err == nil {
		srcBin = b
	}
	if b, err := os.ReadFile(dstBinPath); err == nil {
		dstBin = b
	}

	for _, tc := range repackCases {
		t.Run("Repack/"+tc.Name, func(t *testing.T) {
			expectedErr := parseErr(tc.Err)

			var src, dst []byte
			if tc.Seed != 0 {
				src = make([]byte, tc.SrcLen)
				x1.FillSplitMix64(tc.Seed, src)
				dst = make([]byte, tc.DstLen)
			} else {
				if tc.SrcOffset+tc.SrcLen <= len(srcBin) {
					src = srcBin[tc.SrcOffset : tc.SrcOffset+tc.SrcLen]
				} else {
					src = make([]byte, tc.SrcLen)
				}
				dst = make([]byte, tc.DstLen)
			}

			err := x1.RepackBGRXToRGB(src, tc.W, tc.H, tc.SrcStride, dst, tc.DstStride)
			if expectedErr != nil {
				if !errors.Is(err, expectedErr) {
					t.Fatalf("expected error %v, got %v", expectedErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if tc.DstFNV != 0 {
				fnvVal := x1.FNV1a64(dst)
				if fnvVal != tc.DstFNV {
					t.Fatalf("FNV mismatch: expected %x, got %x", tc.DstFNV, fnvVal)
				}
			} else if tc.DstLen <= len(dstBin)-tc.DstOffset {
				expectedDst := dstBin[tc.DstOffset : tc.DstOffset+tc.DstLen]
				for i := range dst {
					if dst[i] != expectedDst[i] {
						t.Fatalf("dst byte mismatch at %d: expected %x, got %x", i, expectedDst[i], dst[i])
					}
				}
			}
		})
	}

	damageCasesFile := filepath.Join(fixtureDir, "damage_cases.json")
	if damageBytes, err := os.ReadFile(damageCasesFile); err == nil {
		var damageCases []DamageCase
		if err := json.Unmarshal(damageBytes, &damageCases); err == nil {
			for _, tc := range damageCases {
				t.Run("Damage/"+tc.Name, func(t *testing.T) {
					expectedErr := parseErr(tc.Err)
					words, popcount, err := x1.DamageTiles(tc.W, tc.H, tc.S, tc.Rects)
					if expectedErr != nil {
						if !errors.Is(err, expectedErr) {
							t.Fatalf("expected error %v, got %v", expectedErr, err)
						}
						return
					}
					if err != nil {
						t.Fatalf("unexpected error: %v", err)
					}
					if popcount != tc.Popcount {
						t.Fatalf("popcount mismatch: expected %d, got %d", tc.Popcount, popcount)
					}
					if len(words) != len(tc.WordsHex) {
						t.Fatalf("words len mismatch: expected %d, got %d", len(tc.WordsHex), len(words))
					}
					for i, h := range tc.WordsHex {
						expectedHex := fmt.Sprintf("%016x", words[i])
						if expectedHex != h {
							t.Fatalf("word %d mismatch: expected %s, got %s", i, h, expectedHex)
						}
					}
				})
			}
		}
	}
}

func TestHandComputedUnitTests(t *testing.T) {
	// 2x2 repack with padded strides and untouched destination padding
	t.Run("2x2 Repack Padded", func(t *testing.T) {
		w, h := 2, 2
		srcStride := 10 // >= 4*2 = 8
		dstStride := 8  // >= 3*2 = 6

		src := make([]byte, (h-1)*srcStride+4*w+2) // 1*10 + 8 + 2 = 20
		dst := make([]byte, (h-1)*dstStride+3*w+4) // 1*8 + 6 + 4 = 18

		for i := range dst {
			dst[i] = 0xEE
		}

		// Row 0 pixels:
		// x=0: B=0x11, G=0x22, R=0x33, X=0xFF
		// x=1: B=0x44, G=0x55, R=0x66, X=0xFF
		src[0], src[1], src[2], src[3] = 0x11, 0x22, 0x33, 0xFF
		src[4], src[5], src[6], src[7] = 0x44, 0x55, 0x66, 0xFF
		// Row 0 padding: src[8], src[9]

		// Row 1 pixels:
		// x=0: B=0x77, G=0x88, R=0x99, X=0xFF
		// x=1: B=0xAA, G=0xBB, R=0xCC, X=0xFF
		src[10], src[11], src[12], src[13] = 0x77, 0x88, 0x99, 0xFF
		src[14], src[15], src[16], src[17] = 0xAA, 0xBB, 0xCC, 0xFF

		err := x1.RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		// Verify Row 0 dst: 0x33, 0x22, 0x11, 0x66, 0x55, 0x44, padding: dst[6]=0xEE, dst[7]=0xEE
		expectedRow0 := []byte{0x33, 0x22, 0x11, 0x66, 0x55, 0x44}
		for i, v := range expectedRow0 {
			if dst[i] != v {
				t.Fatalf("row 0 mismatch at %d: got %x, expected %x", i, dst[i], v)
			}
		}
		if dst[6] != 0xEE || dst[7] != 0xEE {
			t.Fatalf("row 0 padding touched: %x %x", dst[6], dst[7])
		}

		// Verify Row 1 dst: 0x99, 0x88, 0x77, 0xCC, 0xBB, 0xAA, padding: dst[14]=0xEE, dst[15]=0xEE, dst[16]=0xEE, dst[17]=0xEE
		expectedRow1 := []byte{0x99, 0x88, 0x77, 0xCC, 0xBB, 0xAA}
		for i, v := range expectedRow1 {
			if dst[8+i] != v {
				t.Fatalf("row 1 mismatch at %d: got %x, expected %x", i, dst[8+i], v)
			}
		}
		if dst[14] != 0xEE || dst[15] != 0xEE || dst[16] != 0xEE || dst[17] != 0xEE {
			t.Fatalf("dst padding touched: %v", dst[14:])
		}
	})

	// Every error code
	t.Run("Error Codes Repack", func(t *testing.T) {
		src := make([]byte, 100)
		dst := make([]byte, 100)

		// ErrDim
		if err := x1.RepackBGRXToRGB(src, -1, 10, 40, dst, 30); !errors.Is(err, x1.ErrDim) {
			t.Fatalf("expected ErrDim for w=-1, got %v", err)
		}
		if err := x1.RepackBGRXToRGB(src, 16385, 10, 16385*4, dst, 16385*3); !errors.Is(err, x1.ErrDim) {
			t.Fatalf("expected ErrDim for w=16385, got %v", err)
		}

		// ErrStride
		if err := x1.RepackBGRXToRGB(src, 10, 10, 39, dst, 30); !errors.Is(err, x1.ErrStride) {
			t.Fatalf("expected ErrStride for srcStride < 4W, got %v", err)
		}
		if err := x1.RepackBGRXToRGB(src, 10, 10, 40, dst, 29); !errors.Is(err, x1.ErrStride) {
			t.Fatalf("expected ErrStride for dstStride < 3W, got %v", err)
		}

		// ErrShort
		shortSrc := make([]byte, (2-1)*40+4*10-1) // 79 bytes instead of 80
		if err := x1.RepackBGRXToRGB(shortSrc, 10, 2, 40, dst, 30); !errors.Is(err, x1.ErrShort) {
			t.Fatalf("expected ErrShort for src, got %v", err)
		}
		shortDst := make([]byte, (2-1)*30+3*10-1) // 59 bytes instead of 60
		if err := x1.RepackBGRXToRGB(src, 10, 2, 40, shortDst, 30); !errors.Is(err, x1.ErrShort) {
			t.Fatalf("expected ErrShort for dst, got %v", err)
		}
	})

	t.Run("Error Codes Damage", func(t *testing.T) {
		rects := []x1.Rect{{X: 0, Y: 0, W: 10, H: 10}}

		// ErrDim
		if _, _, err := x1.DamageTiles(0, 10, 32, rects); !errors.Is(err, x1.ErrDim) {
			t.Fatalf("expected ErrDim for w=0, got %v", err)
		}

		// ErrGrid
		if _, _, err := x1.DamageTiles(100, 100, 3, rects); !errors.Is(err, x1.ErrGrid) {
			t.Fatalf("expected ErrGrid for non-power-of-two s=3, got %v", err)
		}
		if _, _, err := x1.DamageTiles(100, 100, 512, rects); !errors.Is(err, x1.ErrGrid) {
			t.Fatalf("expected ErrGrid for s=512, got %v", err)
		}

		// ErrRect
		invalidRects := []x1.Rect{{X: 0, Y: 0, W: -1, H: 10}}
		if _, _, err := x1.DamageTiles(100, 100, 32, invalidRects); !errors.Is(err, x1.ErrRect) {
			t.Fatalf("expected ErrRect for W=-1, got %v", err)
		}
	})

	// Worked check: W 1920, H 1080, s 32 gives gw 60, gh 34, 2040 tiles and 32 words; a 192x32 rectangle at x 96, y 0 sets tiles 3 to 8 of row 0.
	t.Run("Worked Check 1920x1080 s32", func(t *testing.T) {
		w, h, s := 1920, 1080, 32
		rects := []x1.Rect{{X: 96, Y: 0, W: 192, H: 32}}

		words, popcount, err := x1.DamageTiles(w, h, s, rects)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		// gw = ceil(1920/32) = 60, gh = ceil(1080/32) = 34
		// total tiles = 60 * 34 = 2040
		// num words = ceil(2040/64) = 32
		if len(words) != 32 {
			t.Fatalf("expected 32 words, got %d", len(words))
		}

		// rect x: 96 to 96+192 = 288. tx: 96/32=3 to 288/32=9 (exclusive 3..8).
		// rect y: 0 to 32. ty: 0 to 1 (exclusive 0).
		// tiles set: row 0, tiles 3, 4, 5, 6, 7, 8 (6 tiles total)
		if popcount != 6 {
			t.Fatalf("expected popcount 6, got %d", popcount)
		}

		// Check word 0 has bits 3, 4, 5, 6, 7, 8 set
		var expectedWord0 uint64 = (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8)
		if words[0] != expectedWord0 {
			t.Fatalf("word 0 mismatch: expected %x, got %x", expectedWord0, words[0])
		}

		for i := 1; i < len(words); i++ {
			if words[i] != 0 {
				t.Fatalf("word %d expected 0, got %x", i, words[i])
			}
		}
	})

	// x 33 with w 0 setting no bit
	t.Run("Zero Width Rect", func(t *testing.T) {
		rects := []x1.Rect{{X: 33, Y: 10, W: 0, H: 20}}
		words, popcount, err := x1.DamageTiles(100, 100, 16, rects)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if popcount != 0 {
			t.Fatalf("expected popcount 0, got %d", popcount)
		}
		for _, w := range words {
			if w != 0 {
				t.Fatalf("expected word 0, got %x", w)
			}
		}
	})

	// Rectangle crossing partial bottom row
	t.Run("Partial Edge Tiles Rect", func(t *testing.T) {
		// W=10, H=10, s=8 -> gw=2 (0..7, 8..9), gh=2 (0..7, 8..9)
		// total tiles = 4, 1 word
		// rect x=5, y=5, w=5, h=5 -> x1=10, y1=10.
		// tx: 5/8=0 to ceil(10/8)=2 (0, 1).
		// ty: 5/8=0 to ceil(10/8)=2 (0, 1).
		// tiles: (0,0)=0, (0,1)=1, (1,0)=2, (1,1)=3
		rects := []x1.Rect{{X: 5, Y: 5, W: 5, H: 5}}
		words, popcount, err := x1.DamageTiles(10, 10, 8, rects)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if popcount != 4 {
			t.Fatalf("expected popcount 4, got %d", popcount)
		}
		if words[0] != 0x0F {
			t.Fatalf("expected word 0x0F, got %x", words[0])
		}
	})

	// SplitMix64 and FNV-1a 64 against values computed and pinned
	t.Run("SplitMix64 and FNV1a64 Pinned", func(t *testing.T) {
		sm := x1.SplitMix64{State: 0x123456789ABCDEF0}
		v1 := sm.Next()
		buf := make([]byte, 16)
		x1.FillSplitMix64(0x123456789ABCDEF0, buf)

		// Check little-endian decoding of first 8 bytes matches v1
		var firstWord uint64
		for i := 0; i < 8; i++ {
			firstWord |= uint64(buf[i]) << (i * 8)
		}
		if firstWord != v1 {
			t.Fatalf("SplitMix64 buffer mismatch: expected word %x, got %x", v1, firstWord)
		}

		// FNV1a64 over "123456789"
		testData := []byte("123456789")
		// FNV-1a 64-bit hash for "123456789" with offset 0xcbf29ce484222325 and prime 0x100000001b3 is 0x06d5573923c6cdfc
		const expectedFNV uint64 = 0x06d5573923c6cdfc
		fnvVal := x1.FNV1a64(testData)
		if fnvVal != expectedFNV {
			t.Fatalf("FNV-1a 64 mismatch for '123456789': expected %x, got %x", expectedFNV, fnvVal)
		}
	})
}

func BenchmarkRepack(b *testing.B) {
	w, h := 1920, 1080
	srcStride := w * 4
	dstStride := w * 3
	src := make([]byte, h*srcStride)
	dst := make([]byte, h*dstStride)

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = x1.RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	}
}

func BenchmarkDamage(b *testing.B) {
	w, h, s := 1920, 1080, 32
	rects := []x1.Rect{
		{X: 10, Y: 10, W: 200, H: 200},
		{X: 500, Y: 500, W: 300, H: 100},
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, _, _ = x1.DamageTiles(w, h, s, rects)
	}
}
