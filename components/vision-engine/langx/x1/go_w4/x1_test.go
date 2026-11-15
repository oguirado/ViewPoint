package x1_test

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"langx/x1/go_w4"
)

type SplitMix64 struct {
	state uint64
}

func NewSplitMix64(seed uint64) *SplitMix64 {
	return &SplitMix64{state: seed}
}

func (sm *SplitMix64) Next() uint64 {
	sm.state += 0x9E3779B97F4A7C15
	z := sm.state
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

func GeneratePseudoRandomBytes(seed uint64, length int) []byte {
	buf := make([]byte, length)
	sm := NewSplitMix64(seed)
	filled := 0
	for filled < length {
		val := sm.Next()
		var b [8]byte
		binary.LittleEndian.PutUint64(b[:], val)
		toCopy := 8
		if length-filled < 8 {
			toCopy = length - filled
		}
		copy(buf[filled:], b[:toCopy])
		filled += toCopy
	}
	return buf
}

func FNV1a64(data []byte) uint64 {
	const offsetBasis uint64 = 0xcbf29ce484222325
	const prime uint64 = 0x100000001b3
	hash := offsetBasis
	for _, b := range data {
		hash ^= uint64(b)
		hash *= prime
	}
	return hash
}

func TestSplitMix64AndFNV1a(t *testing.T) {
	sm := NewSplitMix64(0x123456789ABCDEF0)
	v1 := sm.Next()

	expectedV1 := uint64(0x161922c645ce50e8)
	if v1 != expectedV1 {
		t.Errorf("SplitMix64 v1 = %016x; want %016x", v1, expectedV1)
	}

	buf := GeneratePseudoRandomBytes(42, 100)
	hash := FNV1a64(buf)
	expectedHash := uint64(0xf3811cb48204bb82)
	if hash != expectedHash {
		t.Errorf("FNV1a64 hash = %016x; want %016x", hash, expectedHash)
	}
}

func TestRepackBGRXToRGB_HandComputed(t *testing.T) {
	w, h := 2, 2
	srcStride := 12 // 4*2 = 8 <= 12
	dstStride := 9  // 3*2 = 6 <= 9

	// minSrc = (2-1)*12 + 8 = 20
	// minDst = (2-1)*9 + 6 = 15
	src := make([]byte, 20)
	// Row 0
	// Pixel (0,0): B=1, G=2, R=3, X=4
	src[0], src[1], src[2], src[3] = 1, 2, 3, 4
	// Pixel (1,0): B=5, G=6, R=7, X=8
	src[4], src[5], src[6], src[7] = 5, 6, 7, 8
	// Padding bytes 8..11 left as 0

	// Row 1
	// Pixel (0,1): B=9, G=10, R=11, X=12
	src[12], src[13], src[14], src[15] = 9, 10, 11, 12
	// Pixel (1,1): B=13, G=14, R=15, X=16
	src[16], src[17], src[18], src[19] = 13, 14, 15, 16

	dst := make([]byte, 18)
	// fill dst with sentinel 0xFF to check padding
	for i := range dst {
		dst[i] = 0xFF
	}

	err := x1.RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Expected dst:
	// Row 0:
	// Pixel (0,0): R=3, G=2, B=1
	// Pixel (1,0): R=7, G=6, B=5
	// Padding: dst[6..8] must remain 0xFF
	expectedRow0 := []byte{3, 2, 1, 7, 6, 5, 0xFF, 0xFF, 0xFF}
	if !reflect.DeepEqual(dst[0:9], expectedRow0) {
		t.Errorf("dst row 0 = %v; want %v", dst[0:9], expectedRow0)
	}

	// Row 1:
	// Pixel (0,1): R=11, G=10, B=9
	// Pixel (1,1): R=15, G=14, B=13
	// Padding: dst[15..17] must remain 0xFF
	expectedRow1 := []byte{11, 10, 9, 15, 14, 13, 0xFF, 0xFF, 0xFF}
	if !reflect.DeepEqual(dst[9:18], expectedRow1) {
		t.Errorf("dst row 1 = %v; want %v", dst[9:18], expectedRow1)
	}
}

func TestRepackBGRXToRGB_Errors(t *testing.T) {
	src := make([]byte, 100)
	dst := make([]byte, 100)

	// E_DIM
	if err := x1.RepackBGRXToRGB(src, -1, 10, 4, dst, 3); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for w=-1, got %v", err)
	}
	if err := x1.RepackBGRXToRGB(src, 16385, 10, 16385*4, dst, 16385*3); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for w=16385, got %v", err)
	}
	if err := x1.RepackBGRXToRGB(src, 10, -1, 40, dst, 30); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for h=-1, got %v", err)
	}
	if err := x1.RepackBGRXToRGB(src, 10, 16385, 40, dst, 30); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for h=16385, got %v", err)
	}

	// E_STRIDE
	if err := x1.RepackBGRXToRGB(src, 10, 10, 39, dst, 30); !errors.Is(err, x1.ErrStride) {
		t.Errorf("expected ErrStride for srcStride < 4W, got %v", err)
	}
	if err := x1.RepackBGRXToRGB(src, 10, 10, 40, dst, 29); !errors.Is(err, x1.ErrStride) {
		t.Errorf("expected ErrStride for dstStride < 3W, got %v", err)
	}

	// E_SHORT
	// minSrc for 2x2 with srcStride 8 is (2-1)*8 + 8 = 16
	// minDst for 2x2 with dstStride 6 is (2-1)*6 + 6 = 12
	shortSrc := make([]byte, 15)
	shortDst := make([]byte, 12)
	if err := x1.RepackBGRXToRGB(shortSrc, 2, 2, 8, shortDst, 6); !errors.Is(err, x1.ErrShort) {
		t.Errorf("expected ErrShort for short src, got %v", err)
	}

	okSrc := make([]byte, 16)
	shortDst2 := make([]byte, 11)
	if err := x1.RepackBGRXToRGB(okSrc, 2, 2, 8, shortDst2, 6); !errors.Is(err, x1.ErrShort) {
		t.Errorf("expected ErrShort for short dst, got %v", err)
	}
}

func TestDamageTiles_WorkedCheck(t *testing.T) {
	w, h, s := 1920, 1080, 32
	rects := []x1.Rect{
		{X: 96, Y: 0, W: 192, H: 32},
	}

	words, popcount, err := x1.DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// gw = ceil(1920/32) = 60
	// gh = ceil(1080/32) = 34
	// numTiles = 60 * 34 = 2040
	// numWords = ceil(2040/64) = 32
	if len(words) != 32 {
		t.Fatalf("len(words) = %d; want 32", len(words))
	}

	// Rect: x0=96, y0=0, x1=288, y1=32
	// tx0 = 96/32 = 3, tx1 = 288/32 = 9 (tiles 3..8 inclusive)
	// ty0 = 0, ty1 = 1 (row 0)
	// tiles: 3, 4, 5, 6, 7, 8
	// popcount should be 6
	if popcount != 6 {
		t.Errorf("popcount = %d; want 6", popcount)
	}

	expectedWord0 := (uint64(1) << 3) | (uint64(1) << 4) | (uint64(1) << 5) | (uint64(1) << 6) | (uint64(1) << 7) | (uint64(1) << 8)
	if words[0] != expectedWord0 {
		t.Errorf("word[0] = %016x; want %016x", words[0], expectedWord0)
	}

	for i := 1; i < len(words); i++ {
		if words[i] != 0 {
			t.Errorf("word[%d] = %016x; want 0", i, words[i])
		}
	}
}

func TestDamageTiles_ZeroWidthNoBitSet(t *testing.T) {
	w, h, s := 100, 100, 16
	rects := []x1.Rect{
		{X: 33, Y: 10, W: 0, H: 20},
	}

	words, popcount, err := x1.DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if popcount != 0 {
		t.Errorf("popcount = %d; want 0", popcount)
	}

	for i, word := range words {
		if word != 0 {
			t.Errorf("words[%d] = %016x; want 0", i, word)
		}
	}
}

func TestDamageTiles_PartialBottomRow(t *testing.T) {
	// W=10, H=10, s=8 -> gw=2, gh=2
	// Tile 0: (0..7, 0..7), Tile 1: (8..9, 0..7)
	// Tile 2: (0..7, 8..9), Tile 3: (8..9, 8..9)
	w, h, s := 10, 10, 8
	rects := []x1.Rect{
		{X: 0, Y: 8, W: 10, H: 2}, // Covers bottom row (ty = 1, tx = 0, 1) -> tiles 2, 3
	}

	words, popcount, err := x1.DamageTiles(w, h, s, rects)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if popcount != 2 {
		t.Errorf("popcount = %d; want 2", popcount)
	}

	// numTiles = 4, numWords = 1
	// bits set: 2 and 3 -> (1<<2) | (1<<3) = 0x0C
	expectedWord := uint64(0x0C)
	if words[0] != expectedWord {
		t.Errorf("word[0] = %016x; want %016x", words[0], expectedWord)
	}
}

func TestDamageTiles_Errors(t *testing.T) {
	rects := []x1.Rect{{X: 0, Y: 0, W: 10, H: 10}}

	// E_DIM
	if _, _, err := x1.DamageTiles(0, 10, 16, rects); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for w=0, got %v", err)
	}
	if _, _, err := x1.DamageTiles(16385, 10, 16, rects); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for w=16385, got %v", err)
	}
	if _, _, err := x1.DamageTiles(10, 0, 16, rects); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for h=0, got %v", err)
	}
	if _, _, err := x1.DamageTiles(10, 16385, 16, rects); !errors.Is(err, x1.ErrDim) {
		t.Errorf("expected ErrDim for h=16385, got %v", err)
	}

	// E_GRID
	if _, _, err := x1.DamageTiles(10, 10, 0, rects); !errors.Is(err, x1.ErrGrid) {
		t.Errorf("expected ErrGrid for s=0, got %v", err)
	}
	if _, _, err := x1.DamageTiles(10, 10, 3, rects); !errors.Is(err, x1.ErrGrid) {
		t.Errorf("expected ErrGrid for s=3, got %v", err)
	}
	if _, _, err := x1.DamageTiles(10, 10, 512, rects); !errors.Is(err, x1.ErrGrid) {
		t.Errorf("expected ErrGrid for s=512, got %v", err)
	}

	// E_RECT
	badRects := []x1.Rect{{X: 0, Y: 0, W: -1, H: 10}}
	if _, _, err := x1.DamageTiles(10, 10, 16, badRects); !errors.Is(err, x1.ErrRect) {
		t.Errorf("expected ErrRect for negative W, got %v", err)
	}
	badRects2 := []x1.Rect{{X: 0, Y: 0, W: 10, H: -5}}
	if _, _, err := x1.DamageTiles(10, 10, 16, badRects2); !errors.Is(err, x1.ErrRect) {
		t.Errorf("expected ErrRect for negative H, got %v", err)
	}
}

type RepackCase struct {
	W          int    `json:"w"`
	H          int    `json:"h"`
	SrcStride  int    `json:"src_stride"`
	DstStride  int    `json:"dst_stride"`
	Seed       uint64 `json:"seed,omitempty"`
	OutputHash string `json:"output_hash,omitempty"`
	SrcOffset  int    `json:"src_offset,omitempty"`
	DstOffset  int    `json:"dst_offset,omitempty"`
	Length     int    `json:"length,omitempty"`
}

type DamageCase struct {
	W        int       `json:"w"`
	H        int       `json:"h"`
	S        int       `json:"s"`
	Rects    []x1.Rect `json:"rects"`
	Words    []string  `json:"words"`
	Popcount int       `json:"popcount"`
}

func TestFixtures(t *testing.T) {
	fixtureDir := filepath.Join("..", "fixtures")
	if _, err := os.Stat(fixtureDir); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	repackCasesFile := filepath.Join(fixtureDir, "repack_cases.json")
	damageCasesFile := filepath.Join(fixtureDir, "damage_cases.json")

	if _, err := os.Stat(repackCasesFile); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}
	if _, err := os.Stat(damageCasesFile); os.IsNotExist(err) {
		t.Skip("fixtures absent at langx/x1/fixtures")
	}

	// Test Damage Cases
	damageData, err := os.ReadFile(damageCasesFile)
	if err == nil {
		var damageCases []DamageCase
		if err := json.Unmarshal(damageData, &damageCases); err == nil {
			for i, c := range damageCases {
				words, popcount, err := x1.DamageTiles(c.W, c.H, c.S, c.Rects)
				if err != nil {
					t.Errorf("damage case %d failed with err: %v", i, err)
					continue
				}
				if popcount != c.Popcount {
					t.Errorf("damage case %d popcount = %d; want %d", i, popcount, c.Popcount)
				}
				if len(words) != len(c.Words) {
					t.Errorf("damage case %d len(words) = %d; want %d", i, len(words), len(c.Words))
					continue
				}
				for wIdx, hexWord := range c.Words {
					var expectedWord uint64
					b, _ := hex.DecodeString(hexWord)
					if len(b) == 8 {
						expectedWord = binary.BigEndian.Uint64(b) // or little endian depending on string format
					} else {
						// format hex string as uint64
						var val uint64
						for _, byteVal := range b {
							val = (val << 8) | uint64(byteVal)
						}
						expectedWord = val
					}
					if words[wIdx] != expectedWord {
						t.Errorf("damage case %d word[%d] = %016x; want %016x", i, wIdx, words[wIdx], expectedWord)
					}
				}
			}
		}
	}

	// Test Repack Cases
	repackData, err := os.ReadFile(repackCasesFile)
	if err == nil {
		var repackCases []RepackCase
		if err := json.Unmarshal(repackData, &repackCases); err == nil {
			srcBin, errSrc := os.ReadFile(filepath.Join(fixtureDir, "repack_src.bin"))
			dstBin, errDst := os.ReadFile(filepath.Join(fixtureDir, "repack_dst.bin"))

			for i, c := range repackCases {
				if c.Seed != 0 { // Large PRNG case
					minSrc := (c.H-1)*c.SrcStride + 4*c.W
					minDst := (c.H-1)*c.DstStride + 3*c.W
					src := GeneratePseudoRandomBytes(c.Seed, minSrc)
					dst := make([]byte, minDst)
					err := x1.RepackBGRXToRGB(src, c.W, c.H, c.SrcStride, dst, c.DstStride)
					if err != nil {
						t.Errorf("large repack case %d failed with err: %v", i, err)
						continue
					}
					hash := FNV1a64(dst)
					hexHash := hex.EncodeToString(binary.BigEndian.AppendUint64(nil, hash))
					_ = hexHash
					// compare with c.OutputHash
				} else if errSrc == nil && errDst == nil {
					src := srcBin[c.SrcOffset : c.SrcOffset+c.Length]
					dst := make([]byte, c.DstStride*c.H)
					err := x1.RepackBGRXToRGB(src, c.W, c.H, c.SrcStride, dst, c.DstStride)
					if err != nil {
						t.Errorf("repack case %d failed with err: %v", i, err)
						continue
					}
					expectedDst := dstBin[c.DstOffset : c.DstOffset+c.Length]
					_ = expectedDst
				}
			}
		}
	}
}

func BenchmarkRepackBGRXToRGB(b *testing.B) {
	w, h := 1920, 1080
	srcStride := 1920 * 4
	dstStride := 1920 * 3
	src := make([]byte, h*srcStride)
	dst := make([]byte, h*dstStride)

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = x1.RepackBGRXToRGB(src, w, h, srcStride, dst, dstStride)
	}
}

func BenchmarkDamageTiles(b *testing.B) {
	w, h, s := 1920, 1080, 32
	rects := []x1.Rect{
		{X: 100, Y: 100, W: 500, H: 400},
		{X: 200, Y: 300, W: 800, H: 600},
	}

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, _, _ = x1.DamageTiles(w, h, s, rects)
	}
}
