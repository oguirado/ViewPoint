package aggregator

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"strings"
	"testing"
)

// Literal C3 vector, copied from test_repro_golden_vector, independent of the encoder.
const c3Golden = `{"affected":["src/a.go"],"assertion":"café: expected 7\nactual 8","base_image_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","base_recipe_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","cache_manifest_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","extract_policy_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","failure_kind":"assertion","frames":[{"line":7,"path":"src/a.go","symbol":"TestA"}],"gate_argv":["go","test","./..."],"gate_cwd":".","guest_kernel_sha256":null,"guest_rootfs_sha256":null,"normalized_exit":5,"normalizer":"w8-repro/1","repo":"example/repo","repro_steps":["open","save"]}`
const c3Digest = "4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934"

func TestC3GoldenIdentity(t *testing.T) {
	id, err := ParseAndComputeIdentity([]byte(c3Golden), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(c3Golden) != 761 || !bytes.Equal(id.CanonicalRecord, []byte(c3Golden)) || id.ReproSHA256 != c3Digest {
		t.Fatalf("C3 literal mismatch: bytes=%s digest=%s", id.CanonicalRecord, id.ReproSHA256)
	}
	old := append([]byte("w8-repro-v1"), make([]byte, 8)...)
	binary.BigEndian.PutUint64(old[len("w8-repro-v1"):], uint64(len(c3Golden)))
	old = append(old, c3Golden...)
	if bytes.Equal(old, id.CanonicalRecord) || DefaultHash(old) == id.ReproSHA256 {
		t.Fatal("legacy framing accepted")
	}
}

func TestC3RequiredRefusals(t *testing.T) {
	for name, raw := range map[string]string{
		"old_version":    strings.Replace(c3Golden, "w8-repro/1", "w8-repro-v1", 1),
		"unpaired_guest": strings.Replace(c3Golden, `"guest_kernel_sha256":null`, `"guest_kernel_sha256":"`+strings.Repeat("d", 64)+`"`, 1),
		"float_exit":     strings.Replace(c3Golden, `"normalized_exit":5`, `"normalized_exit":5.0`, 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseAndComputeIdentity([]byte(raw), nil); err == nil {
				t.Fatal("unsafe identity accepted")
			}
		})
	}
}

func TestC3TypedVolatilityAndOrderedFrames(t *testing.T) {
	first, complete, err := ParseReproRecord([]byte(c3Golden))
	if err != nil || !complete {
		t.Fatalf("fixture: complete=%v err=%v", complete, err)
	}
	first.WorkspacePrefix = "/workspace/repo/"
	first.PID, first.TimeMS = 23, 45
	first.Frames[0].Path = "/workspace/repo/src/a.go"
	first.Frames[0].Address = "0xabc"
	second := cloneReproRecord(first)
	second.WorkspacePrefix = "/workspace/retry/"
	second.PID, second.TimeMS = 99, 100
	second.Frames[0].Path = "/workspace/retry/src/a.go"
	second.Frames[0].Address = "0xdef"
	for _, record := range []ReproRecord{first, second} {
		raw, err := json.Marshal(record)
		if err != nil {
			t.Fatal(err)
		}
		id, err := ParseAndComputeIdentity(raw, nil)
		if err != nil {
			t.Fatal(err)
		}
		if id.ReproSHA256 != c3Digest || !bytes.Equal(id.CanonicalRecord, []byte(c3Golden)) {
			t.Fatal("typed volatility changed C3 identity")
		}
	}
	first.Frames = append(first.Frames, ReproFrame{Path: "src/b.go", Symbol: "TestB", Line: 9})
	second = cloneReproRecord(first)
	second.Frames[0], second.Frames[1] = second.Frames[1], second.Frames[0]
	a, err := ComputeIdentity(first, nil)
	if err != nil {
		t.Fatal(err)
	}
	b, err := ComputeIdentity(second, nil)
	if err != nil {
		t.Fatal(err)
	}
	if a.ReproSHA256 == b.ReproSHA256 || EqualRepro(&a, &b) {
		t.Fatal("ordered frames merged")
	}
}

func TestC3ClosedTypesAndUnicode(t *testing.T) {
	for name, raw := range map[string]string{
		"duplicate_nested":    strings.Replace(c3Golden, `"line":7`, `"line":7,"line":8`, 1),
		"unknown_nested":      strings.Replace(c3Golden, `"line":7`, `"line":7,"extra":8`, 1),
		"case_variant_nested": strings.Replace(c3Golden, `"path":"src/a.go"`, `"Path":"src/a.go"`, 1),
		"invalid_utf8":        strings.Replace(c3Golden, "café", string([]byte{0xff}), 1),
		"lone_surrogate":      strings.Replace(c3Golden, "café", `\ud800`, 1),
		"float_frame":         strings.Replace(c3Golden, `"line":7`, `"line":7.5`, 1),
		"bool_exit":           strings.Replace(c3Golden, `"normalized_exit":5`, `"normalized_exit":true`, 1),
		"null_volatile":       strings.TrimSuffix(c3Golden, "}") + `,"pid":null}`,
		"bool_volatile":       strings.TrimSuffix(c3Golden, "}") + `,"pid":true}`,
		"null_address":        strings.Replace(c3Golden, `"line":7`, `"line":7,"address":null`, 1),
		"missing_guest":       strings.Replace(c3Golden, `"guest_rootfs_sha256":null,`, "", 1),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := ParseAndComputeIdentity([]byte(raw), nil)
			if err == nil || ExitCode(err) < 2 || ExitCode(err) > 7 {
				t.Fatalf("expected normalized refusal, got %v", err)
			}
		})
	}
	rec, _, err := ParseReproRecord([]byte(c3Golden))
	if err != nil {
		t.Fatal(err)
	}
	rec.Assertion = string([]byte{0xff})
	if _, err := ComputeIdentity(rec, nil); err == nil {
		t.Fatal("typed invalid Unicode accepted")
	}
}

func TestC3EmptyListsAndGuestPins(t *testing.T) {
	rec, _, err := ParseReproRecord([]byte(c3Golden))
	if err != nil {
		t.Fatal(err)
	}
	rec.Affected = []string{}
	rec.Frames = []ReproFrame{}
	kernel, rootfs := strings.Repeat("d", 64), strings.Repeat("e", 64)
	rec.GuestKernelSHA256, rec.GuestRootfsSHA256 = &kernel, &rootfs
	id, err := ComputeIdentity(rec, nil)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseAndComputeIdentity(id.CanonicalRecord, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !EqualRepro(&id, &parsed) {
		t.Fatal("empty lists or paired guest pins lost on round trip")
	}
	changed := cloneReproRecord(rec)
	*changed.GuestKernelSHA256 = strings.Repeat("f", 64)
	other, err := ComputeIdentity(changed, nil)
	if err != nil {
		t.Fatal(err)
	}
	if EqualRepro(&id, &other) {
		t.Fatal("guest kernel change merged")
	}
	if *rec.GuestKernelSHA256 != kernel {
		t.Fatal("clone aliased guest pin")
	}
}
