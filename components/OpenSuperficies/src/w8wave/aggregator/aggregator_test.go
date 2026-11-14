package aggregator

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/workerbridge"
)

func sampleReproRecord() ReproRecord {
	return ReproRecord{
		Normalizer:          ReproVersion,
		Repo:                "oguirado/OpenSuperficies",
		Commit:              strings.Repeat("a", 40),
		BaseImageDigest:     strings.Repeat("b", 64),
		CacheManifestSHA256: strings.Repeat("c", 64),
		GateArgv:            []string{"go", "test", "./src/w8wave/aggregator/..."},
		GateCwd:             ".",
		FailureKind:         "gate",
		Assertion:           "FAIL: TestWidget: expected 42, got 0",
		Frames: []ReproFrame{
			{Path: "src/widget/widget.go", Line: 108, Symbol: "Widget.Compute"},
			{Path: "src/widget/widget_test.go", Line: 44, Symbol: "TestWidget"},
		},
		Affected:         []string{"src/widget/widget.go"},
		BaseRecipeSHA256: strings.Repeat("d", 64), ExtractPolicySHA256: strings.Repeat("e", 64),
		NormalizedExit: int64Ptr(5), ReproSteps: []string{"run gate"},
	}
}

func int64Ptr(v int64) *int64 { return &v }
func intPtr(v int) *int       { return &v }

func digestHex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func TestReproIdentityFramingAndCanonicalJSON(t *testing.T) {
	rec := sampleReproRecord()
	id, err := ComputeIdentity(rec, nil)
	if err != nil {
		t.Fatalf("ComputeIdentity failed: %v", err)
	}
	if !bytes.Equal(id.FramedBytes, id.CanonicalRecord) {
		t.Fatal("C3 bytes must have no framing")
	}
	wantSHA := sha256.Sum256(id.FramedBytes)
	if id.ReproSHA256 != hex.EncodeToString(wantSHA[:]) {
		t.Fatalf("repro_sha256 %s != expected %s", id.ReproSHA256, hex.EncodeToString(wantSHA[:]))
	}

	// Parsing from canonical JSON bytes must produce the exact same identity.
	parsedID, err := ParseAndComputeIdentity(id.CanonicalRecord, nil)
	if err != nil {
		t.Fatalf("ParseAndComputeIdentity failed: %v", err)
	}
	if !EqualRepro(&id, &parsedID) {
		t.Fatalf("parsed identity mismatch: %s vs %s", id.ReproSHA256, parsedID.ReproSHA256)
	}
}

func TestIdentityRejectsDuplicateKeysFloatsNonFiniteAndUnknownFields(t *testing.T) {
	base := sampleReproRecord()
	id, err := ComputeIdentity(base, nil)
	if err != nil {
		t.Fatal(err)
	}
	validJSON := string(id.CanonicalRecord)

	badInputs := []struct {
		name string
		raw  string
	}{
		{
			name: "duplicate_key",
			raw:  `{"normalizer":"w8-repro-v1","normalizer":"w8-repro-v1"}`,
		},
		{
			name: "float_number",
			raw:  strings.Replace(validJSON, `"failure_kind":"gate"`, `"failure_kind":1.5`, 1),
		},
		{
			name: "exponent_number",
			raw:  strings.Replace(validJSON, `"failure_kind":"gate"`, `"failure_kind":1e2`, 1),
		},
		{
			name: "non_finite_nan",
			raw:  `{"normalizer":NaN}`,
		},
		{
			name: "non_finite_infinity",
			raw:  `{"normalizer":Infinity}`,
		},
		{
			name: "unknown_field",
			raw:  strings.TrimSuffix(validJSON, "}") + `,"unknown_extra":"value"}`,
		},
		{
			name: "provenance_task_id_in_identity",
			raw:  strings.TrimSuffix(validJSON, "}") + `,"task_id":"task-1"}`,
		},
		{
			name: "provenance_worker_id_in_identity",
			raw:  strings.TrimSuffix(validJSON, "}") + `,"worker_id":"worker-1"}`,
		},
		{
			name: "provenance_cost_in_identity",
			raw:  strings.TrimSuffix(validJSON, "}") + `,"cost_micro_usd":500}`,
		},
		{
			name: "provenance_pulse_in_identity",
			raw:  strings.TrimSuffix(validJSON, "}") + `,"pulse":3}`,
		},
	}

	for _, tc := range badInputs {
		t.Run(tc.name, func(t *testing.T) {
			_, _, err := ParseReproRecord([]byte(tc.raw))
			rc := ExitCode(err)
			if err == nil || (rc != 2 && rc != 5) {
				t.Fatalf("expected refusal rc 2 or 5 for %s, got err=%v rc=%d", tc.name, err, rc)
			}
		})
	}
}

func TestNoUnicodeNormalizationOrWhitespaceFolding(t *testing.T) {
	recNFC := sampleReproRecord()
	recNFC.Assertion = "caf\u00e9 assertion failure"

	recNFD := sampleReproRecord()
	recNFD.Assertion = "cafe\u0301 assertion failure"

	idNFC, err := ComputeIdentity(recNFC, nil)
	if err != nil {
		t.Fatal(err)
	}
	idNFD, err := ComputeIdentity(recNFD, nil)
	if err != nil {
		t.Fatal(err)
	}
	if idNFC.ReproSHA256 == idNFD.ReproSHA256 || bytes.Equal(idNFC.CanonicalRecord, idNFD.CanonicalRecord) {
		t.Fatal("Unicode NFC and NFD records erroneously normalized to identical identity")
	}

	recSingleSpace := sampleReproRecord()
	recSingleSpace.Assertion = "assert x == 1"
	recDoubleSpace := sampleReproRecord()
	recDoubleSpace.Assertion = "assert  x == 1"

	idSingle, err := ComputeIdentity(recSingleSpace, nil)
	if err != nil {
		t.Fatal(err)
	}
	idDouble, err := ComputeIdentity(recDoubleSpace, nil)
	if err != nil {
		t.Fatal(err)
	}
	if idSingle.ReproSHA256 == idDouble.ReproSHA256 || bytes.Equal(idSingle.CanonicalRecord, idDouble.CanonicalRecord) {
		t.Fatal("whitespace-distinct records erroneously folded to identical identity")
	}
}

func TestMustFailForcedCollisionMerge(t *testing.T) {
	forcedHash := strings.Repeat("f", 64)
	collisionHashFn := func([]byte) string { return forcedHash }

	rec1 := sampleReproRecord()
	rec1.Assertion = "assertion alpha"
	rec1.Frames[0].Symbol = "Alpha"

	rec2 := sampleReproRecord()
	rec2.Assertion = "assertion beta"
	rec2.Frames[0].Symbol = "Beta"

	id1, err := ComputeIdentity(rec1, collisionHashFn)
	if err != nil {
		t.Fatal(err)
	}
	id2, err := ComputeIdentity(rec2, collisionHashFn)
	if err != nil {
		t.Fatal(err)
	}
	if id1.ReproSHA256 != forcedHash || id2.ReproSHA256 != forcedHash {
		t.Fatal("expected forced collision hash")
	}
	if bytes.Equal(id1.CanonicalRecord, id2.CanonicalRecord) {
		t.Fatal("distinct records produced equal canonical bytes")
	}

	// Must-FAIL: directly merging two colliding manifestations with different canonical bytes must fail with rc 5.
	m1 := Manifestation{
		ReproSHA256:     id1.ReproSHA256,
		CanonicalRecord: id1.CanonicalRecord,
		FramedBytes:     id1.FramedBytes,
		Record:          rec1,
	}
	m2 := Manifestation{
		ReproSHA256:     id2.ReproSHA256,
		CanonicalRecord: id2.CanonicalRecord,
		FramedBytes:     id2.FramedBytes,
		Record:          rec2,
	}
	if _, err := MergeManifestations(m1, m2); ExitCode(err) != 5 {
		t.Fatalf("forced collision merge must fail with rc 5, got err=%v rc=%d", err, ExitCode(err))
	}

	// Store and Aggregator must keep colliding records separate in the bucket.
	agg, err := NewAggregator(Config{WaveID: "wave-collision", BaseSeq: 0, HashFn: collisionHashFn})
	if err != nil {
		t.Fatal(err)
	}
	obs1 := Observation{
		WaveID:   "wave-collision",
		Seq:      1,
		BodyRef:  digestHex("body-1"),
		Identity: &rec1,
		Provenance: Provenance{
			TaskID:    "task-1",
			AttemptID: "a1",
			LeaseID:   "lease-1",
			Sender:    "worker-a",
		},
	}
	obs2 := Observation{
		WaveID:   "wave-collision",
		Seq:      2,
		BodyRef:  digestHex("body-2"),
		Identity: &rec2,
		Provenance: Provenance{
			TaskID:    "task-2",
			AttemptID: "a1",
			LeaseID:   "lease-2",
			Sender:    "worker-a",
		},
	}
	if err := agg.Ingest(obs1); err != nil {
		t.Fatal(err)
	}
	if err := agg.Ingest(obs2); err != nil {
		t.Fatal(err)
	}

	bucket := agg.Store().Bucket(forcedHash)
	if len(bucket) != 2 {
		t.Fatalf("expected 2 separate entries in collision bucket, got %d", len(bucket))
	}
	summary := agg.Summary()
	if summary.UniqueManifestationCount != 2 {
		t.Fatalf("expected UniqueManifestationCount=2 under forced hash collision, got %d", summary.UniqueManifestationCount)
	}
}

func TestMustFailNullReproEqualityAndUnkeyedObservations(t *testing.T) {
	// Missing required identity field must not produce a shared or null hash.
	incomplete := sampleReproRecord()
	incomplete.Assertion = ""
	if _, err := ComputeIdentity(incomplete, nil); ExitCode(err) != 2 {
		t.Fatalf("expected rc 2 for incomplete ReproRecord, got %v (%d)", err, ExitCode(err))
	}
	if _, err := ParseAndComputeIdentity([]byte(`{"normalizer":"w8-repro-v1"}`), nil); ExitCode(err) != 2 {
		t.Fatalf("expected rc 2 for incomplete JSON record, got %v (%d)", err, ExitCode(err))
	}

	// Must-FAIL: null/unkeyed repro identities must never compare equal or merge.
	if EqualRepro(nil, nil) {
		t.Fatal("nil repro identities must not be equal")
	}
	emptyID := ReproIdentity{}
	if EqualRepro(&emptyID, &emptyID) {
		t.Fatal("empty unkeyed repro identities must not be equal")
	}
	if _, err := MergeManifestations(Manifestation{}, Manifestation{}); ExitCode(err) != 5 {
		t.Fatalf("merging unkeyed manifestations must fail with rc 5, got %v", err)
	}

	agg, err := NewAggregator(Config{WaveID: "wave-unkeyed", BaseSeq: 0})
	if err != nil {
		t.Fatal(err)
	}

	// Claiming a repro_sha256 on an unkeyed observation must be refused.
	badUnkeyed := Observation{
		WaveID:             "wave-unkeyed",
		Seq:                1,
		BodyRef:            digestHex("unkeyed-bad"),
		ClaimedReproSHA256: digestHex("fake-hash"),
		Identity:           &incomplete,
		Provenance:         Provenance{TaskID: "t1", AttemptID: "a1", LeaseID: "l1", Sender: "w1"},
	}
	if err := agg.Ingest(badUnkeyed); ExitCode(err) != 5 {
		t.Fatalf("unkeyed observation claiming a hash must fail with rc 5, got %v", err)
	}

	// Two unkeyed observations must remain separate pending unkeyed observations, never merged.
	u1 := Observation{
		WaveID:     "wave-unkeyed",
		Seq:        1,
		BodyRef:    digestHex("unkeyed-1"),
		Identity:   &incomplete,
		Provenance: Provenance{TaskID: "t1", AttemptID: "a1", LeaseID: "l1", Sender: "w1"},
		Summary:    "missing assertion 1",
	}
	u2 := Observation{
		WaveID:     "wave-unkeyed",
		Seq:        2,
		BodyRef:    digestHex("unkeyed-2"),
		Identity:   nil,
		Provenance: Provenance{TaskID: "t2", AttemptID: "a1", LeaseID: "l2", Sender: "w1"},
		Summary:    "missing assertion 2",
	}
	if err := agg.Ingest(u1); err != nil {
		t.Fatal(err)
	}
	if err := agg.Ingest(u2); err != nil {
		t.Fatal(err)
	}

	sum := agg.Summary()
	if sum.UniqueManifestationCount != 0 {
		t.Fatalf("unkeyed observations must not count as keyed manifestations, got %d", sum.UniqueManifestationCount)
	}
	if len(sum.UnkeyedObservations) != 2 {
		t.Fatalf("expected 2 distinct unkeyed observations, got %d", len(sum.UnkeyedObservations))
	}
}

func TestMustFailLostFailedAttempt(t *testing.T) {
	agg, err := NewAggregator(Config{WaveID: "wave-attempts", BaseSeq: 0})
	if err != nil {
		t.Fatal(err)
	}
	rec := sampleReproRecord()

	failedReceipt := AttemptReceiptRef{
		TaskID:        "task-retry",
		AttemptID:     "a1",
		LeaseID:       "lease-a1",
		ReceiptSHA256: digestHex("receipt-a1-fail"),
		ExitCode:      5,
		FailureReason: "gate",
		StartedAtMS:   int64Ptr(1000),
		FinishedAtMS:  int64Ptr(1500),
		CostMicroUSD:  int64Ptr(20000),
	}
	succeededReceipt := AttemptReceiptRef{
		TaskID:        "task-retry",
		AttemptID:     "a2",
		LeaseID:       "lease-a2",
		ReceiptSHA256: digestHex("receipt-a2-pass"),
		ExitCode:      0,
		StartedAtMS:   int64Ptr(1600),
		FinishedAtMS:  int64Ptr(2200),
		CostMicroUSD:  int64Ptr(30000),
	}

	obs1 := Observation{
		WaveID:         "wave-attempts",
		Seq:            1,
		BodyRef:        digestHex("body-a1"),
		Identity:       &rec,
		Provenance:     Provenance{TaskID: "task-retry", AttemptID: "a1", LeaseID: "lease-a1", Sender: "s1"},
		AttemptReceipt: &failedReceipt,
	}
	obs2 := Observation{
		WaveID:         "wave-attempts",
		Seq:            2,
		BodyRef:        digestHex("body-a2"),
		Identity:       &rec,
		Provenance:     Provenance{TaskID: "task-retry", AttemptID: "a2", LeaseID: "lease-a2", Sender: "s1"},
		AttemptReceipt: &succeededReceipt,
	}
	if err := agg.Ingest(obs1); err != nil {
		t.Fatal(err)
	}
	if err := agg.Ingest(obs2); err != nil {
		t.Fatal(err)
	}

	sum := agg.Summary()
	if len(sum.AttemptReceipts) != 2 {
		t.Fatalf("failed attempt a1 was lost; expected 2 attempt receipts, got %d", len(sum.AttemptReceipts))
	}
	if sum.AttemptReceipts[0].AttemptID != "a1" || sum.AttemptReceipts[0].ExitCode != 5 {
		t.Fatalf("failed attempt a1 missing or altered: %+v", sum.AttemptReceipts[0])
	}
	if sum.AttemptReceipts[1].AttemptID != "a2" || sum.AttemptReceipts[1].ExitCode != 0 {
		t.Fatalf("succeeded attempt a2 missing or altered: %+v", sum.AttemptReceipts[1])
	}
	if sum.TotalCostMicroUSD == nil || *sum.TotalCostMicroUSD != 50000 {
		t.Fatalf("expected total cost 50000 across failed + succeeded attempts, got %v", sum.TotalCostMicroUSD)
	}
	if sum.WallTimeMS == nil || *sum.WallTimeMS != 1200 {
		t.Fatalf("expected wall time 1200 ms (2200 - 1000), got %v", sum.WallTimeMS)
	}

	// Must-FAIL: attempting to overwrite failed attempt a1 with a different receipt must fail with rc 5.
	overwriteA1 := failedReceipt
	overwriteA1.ReceiptSHA256 = digestHex("receipt-a1-rewritten")
	overwriteA1.ExitCode = 0
	if err := agg.RecordAttempt(overwriteA1); ExitCode(err) != 5 {
		t.Fatalf("overwriting existing attempt receipt must fail with rc 5, got %v", err)
	}
}

func TestMustFailWorkerIDChangingIdentityAndPositiveVolatileProvenance(t *testing.T) {
	rec := sampleReproRecord()
	idBase, err := ComputeIdentity(rec, nil)
	if err != nil {
		t.Fatal(err)
	}

	agg, err := NewAggregator(Config{WaveID: "wave-prov", BaseSeq: 0})
	if err != nil {
		t.Fatal(err)
	}

	workers := []Provenance{
		{
			TaskID:       "task-alpha",
			AttemptID:    "a1",
			LeaseID:      "lease-alpha",
			WorkerID:     "worker-01",
			Sender:       "seat-01",
			StartedAtMS:  int64Ptr(100),
			FinishedAtMS: int64Ptr(200),
			CostMicroUSD: int64Ptr(15000),
			Pulse:        1,
		},
		{
			TaskID:       "task-beta",
			AttemptID:    "a2",
			LeaseID:      "lease-beta",
			WorkerID:     "worker-99",
			Sender:       "seat-02",
			StartedAtMS:  int64Ptr(500),
			FinishedAtMS: int64Ptr(900),
			CostMicroUSD: int64Ptr(22000),
			Pulse:        9,
		},
	}

	for i, p := range workers {
		obs := Observation{
			WaveID:             "wave-prov",
			StreamKey:          p.Sender,
			Seq:                1,
			BodyRef:            digestHex("prov-body-" + p.WorkerID),
			ClaimedReproSHA256: idBase.ReproSHA256,
			Identity:           &rec,
			Provenance:         p,
		}
		if err := agg.Ingest(obs); err != nil {
			t.Fatalf("worker %d ingest failed: %v", i, err)
		}
	}

	sum := agg.Summary()
	if sum.UniqueManifestationCount != 1 {
		t.Fatalf("volatile provenance or worker_id changed identity: got %d manifestations, want 1", sum.UniqueManifestationCount)
	}
	if len(sum.Manifestations[0].Observations) != 2 {
		t.Fatalf("expected 2 merged observations in manifestation, got %d", len(sum.Manifestations[0].Observations))
	}
}

func TestMustFailReorderedFramesMerging(t *testing.T) {
	rec1 := sampleReproRecord()
	rec1.Frames = []ReproFrame{{Path: "frame/one.go", Line: 10, Symbol: "One"}, {Path: "frame/two.go", Line: 20, Symbol: "Two"}}

	rec2 := sampleReproRecord()
	rec2.Frames = []ReproFrame{{Path: "frame/two.go", Line: 20, Symbol: "Two"}, {Path: "frame/one.go", Line: 10, Symbol: "One"}}

	id1, err := ComputeIdentity(rec1, nil)
	if err != nil {
		t.Fatal(err)
	}
	id2, err := ComputeIdentity(rec2, nil)
	if err != nil {
		t.Fatal(err)
	}
	if id1.ReproSHA256 == id2.ReproSHA256 || EqualRepro(&id1, &id2) {
		t.Fatal("reordered frames erroneously produced identical repro_sha256")
	}

	agg, err := NewAggregator(Config{WaveID: "wave-frames", BaseSeq: 0})
	if err != nil {
		t.Fatal(err)
	}
	if err := agg.Ingest(Observation{
		WaveID:     "wave-frames",
		Seq:        1,
		BodyRef:    digestHex("frames-1"),
		Identity:   &rec1,
		Provenance: Provenance{TaskID: "t1", AttemptID: "a1", LeaseID: "l1", Sender: "s1"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := agg.Ingest(Observation{
		WaveID:     "wave-frames",
		Seq:        2,
		BodyRef:    digestHex("frames-2"),
		Identity:   &rec2,
		Provenance: Provenance{TaskID: "t2", AttemptID: "a1", LeaseID: "l2", Sender: "s1"},
	}); err != nil {
		t.Fatal(err)
	}

	if agg.Summary().UniqueManifestationCount != 2 {
		t.Fatalf("reordered frames merged in store; expected 2 manifestations, got %d", agg.Summary().UniqueManifestationCount)
	}
}

func TestMustFailGapAcknowledgedAndContiguousSequencePipeline(t *testing.T) {
	agg, err := NewAggregator(Config{WaveID: "wave-seq", BaseSeq: 0})
	if err != nil {
		t.Fatal(err)
	}
	rec := sampleReproRecord()
	ref1 := digestHex("seq-body-1")
	ref2 := digestHex("seq-body-2")
	ref3 := digestHex("seq-body-3")

	obs1 := Observation{
		WaveID:     "wave-seq",
		StreamKey:  "stream-a",
		Seq:        1,
		BodyRef:    ref1,
		Identity:   &rec,
		Provenance: Provenance{TaskID: "t1", AttemptID: "a1", LeaseID: "l1", Sender: "w1"},
		Summary:    "seq 1",
	}
	obs3 := Observation{
		WaveID:     "wave-seq",
		StreamKey:  "stream-a",
		Seq:        3,
		BodyRef:    ref3,
		Identity:   &rec,
		Provenance: Provenance{TaskID: "t1", AttemptID: "a1", LeaseID: "l1", Sender: "w1"},
		Summary:    "seq 3 after gap",
	}

	if err := agg.Ingest(obs1); err != nil {
		t.Fatal(err)
	}
	if err := agg.Ingest(obs3); err != nil {
		t.Fatal(err)
	}

	// Seq 1 is contiguous and acknowledged; Seq 3 is behind gap at 2 and must NOT be acknowledged.
	if !agg.Acknowledged(ref1) {
		t.Fatal("expected ref1 (seq 1) to be acknowledged")
	}
	if agg.Acknowledged(ref3) {
		t.Fatal("ref3 (seq 3) behind a sequence gap must NOT be acknowledged")
	}
	if err := agg.VerifyAcknowledged(ref3); ExitCode(err) != 5 {
		t.Fatalf("VerifyAcknowledged on gap-blocked ref3 must fail with rc 5, got %v", err)
	}

	sum := agg.Summary()
	if len(sum.MissingIntervals) != 1 || sum.MissingIntervals[0].From != 2 || sum.MissingIntervals[0].To != 2 {
		t.Fatalf("expected missing interval [2, 2], got %+v", sum.MissingIntervals)
	}

	// Same sequence with different bytes must be rejected with rc 5.
	obs1Different := obs1
	obs1Different.Summary = "tampered seq 1"
	if err := agg.Ingest(obs1Different); ExitCode(err) != 5 {
		t.Fatalf("same sequence with different bytes must fail with rc 5, got %v", err)
	}

	// Same sequence with identical bytes is an idempotent no-op.
	if err := agg.Ingest(obs1); err != nil {
		t.Fatalf("redelivery of identical seq 1 failed: %v", err)
	}

	// Now deliver Seq 2 to close the gap; both ref2 and ref3 must become acknowledged.
	obs2 := Observation{
		WaveID:     "wave-seq",
		StreamKey:  "stream-a",
		Seq:        2,
		BodyRef:    ref2,
		Identity:   &rec,
		Provenance: Provenance{TaskID: "t1", AttemptID: "a1", LeaseID: "l1", Sender: "w1"},
		Summary:    "seq 2 fills gap",
	}
	if err := agg.Ingest(obs2); err != nil {
		t.Fatal(err)
	}
	if !agg.Acknowledged(ref2) || !agg.Acknowledged(ref3) {
		t.Fatal("expected both ref2 and ref3 to be acknowledged after gap was filled")
	}
	sumAfter := agg.Summary()
	if len(sumAfter.MissingIntervals) != 0 {
		t.Fatalf("expected no missing intervals after gap filled, got %+v", sumAfter.MissingIntervals)
	}
}

func TestQuotedByteAnchorsVerifiedAgainstEvidenceSegmentDigest(t *testing.T) {
	segment := []byte("line 1: setup ok\nFAIL: TestWidget: expected 42, got 0\n\tsrc/widget/widget.go:108\n")
	segSHA := DefaultHash(segment)

	quote := "FAIL: TestWidget: expected 42, got 0"
	start := strings.Index(string(segment), quote)
	end := start + len(quote)

	if err := VerifyAnchors(segment, segSHA, []ByteAnchor{
		{Quote: quote, StartByte: intPtr(start), EndByte: intPtr(end)},
		{Quote: "src/widget/widget.go:108"},
	}); err != nil {
		t.Fatalf("expected valid anchors to pass, got %v", err)
	}

	// Mismatched evidence segment digest must fail with rc 5.
	if err := VerifyAnchors(segment, digestHex("wrong-segment"), []ByteAnchor{{Quote: quote}}); ExitCode(err) != 5 {
		t.Fatalf("expected rc 5 on segment digest mismatch, got %v", err)
	}

	// Mutated quote not present in segment must fail with rc 5.
	if err := VerifyAnchors(segment, segSHA, []ByteAnchor{{Quote: "expected 43, got 0"}}); ExitCode(err) != 5 {
		t.Fatalf("expected rc 5 on mutated anchor quote, got %v", err)
	}

	// Wrong byte range offsets must fail with rc 5.
	if err := VerifyAnchors(segment, segSHA, []ByteAnchor{
		{Quote: quote, StartByte: intPtr(start + 1), EndByte: intPtr(end)},
	}); ExitCode(err) != 5 {
		t.Fatalf("expected rc 5 on wrong anchor offsets, got %v", err)
	}
}

func TestMustFailMissingCostAsZeroAndWaveSummary(t *testing.T) {
	agg, err := NewAggregator(Config{WaveID: "wave-cost", BaseSeq: 0})
	if err != nil {
		t.Fatal(err)
	}

	// Must-FAIL: recording unavailable cost as zero must be refused with rc 5.
	zero := int64(0)
	if err := agg.RecordAttempt(AttemptReceiptRef{
		TaskID:                "t-cost",
		AttemptID:             "a1",
		LeaseID:               "l-cost-1",
		ReceiptSHA256:         digestHex("r-cost-bad"),
		ExitCode:              0,
		CostMicroUSD:          &zero,
		CostUnavailableAsZero: true,
	}); ExitCode(err) != 5 {
		t.Fatalf("missing cost encoded as zero must fail with rc 5, got %v", err)
	}
	if err := agg.RecordAttempt(AttemptReceiptRef{
		TaskID:        "t-cost",
		AttemptID:     "a1",
		LeaseID:       "l-cost-1",
		ReceiptSHA256: digestHex("r-cost-bad"),
		ExitCode:      0,
		CostMicroUSD:  &zero,
		CostReason:    ReasonAusente,
	}); ExitCode(err) != 5 {
		t.Fatalf("cost with reason AUSENTE and value 0 must fail with rc 5, got %v", err)
	}

	// When one attempt has known cost and another has unavailable (nil) cost,
	// WaveSummary.TotalCostMicroUSD must be nil (JSON null) with CostReason "AUSENTE", never 0.
	if err := agg.RecordAttempt(AttemptReceiptRef{
		TaskID:        "t-cost-1",
		AttemptID:     "a1",
		LeaseID:       "l-cost-1",
		ReceiptSHA256: digestHex("r-cost-1"),
		ExitCode:      0,
		StartedAtMS:   int64Ptr(10),
		FinishedAtMS:  int64Ptr(50),
		CostMicroUSD:  int64Ptr(12000),
	}); err != nil {
		t.Fatal(err)
	}
	if err := agg.RecordAttempt(AttemptReceiptRef{
		TaskID:        "t-cost-2",
		AttemptID:     "a1",
		LeaseID:       "l-cost-2",
		ReceiptSHA256: digestHex("r-cost-2"),
		ExitCode:      5,
		FailureReason: "gate",
		StartedAtMS:   int64Ptr(20),
		FinishedAtMS:  int64Ptr(80),
		CostMicroUSD:  nil,
		CostReason:    ReasonAusente,
	}); err != nil {
		t.Fatal(err)
	}

	sum := agg.Summary()
	if sum.TotalCostMicroUSD != nil {
		t.Fatalf("expected nil TotalCostMicroUSD when an attempt cost is unavailable, got %v", *sum.TotalCostMicroUSD)
	}
	if sum.CostReason != ReasonAusente {
		t.Fatalf("expected CostReason=%q, got %q", ReasonAusente, sum.CostReason)
	}
	if err := ValidateSummary(sum); err != nil {
		t.Fatalf("ValidateSummary failed on valid summary: %v", err)
	}

	// Must-FAIL: tampering with the summary to set TotalCostMicroUSD = 0 when an attempt has nil cost must fail.
	tampered := sum
	tampered.TotalCostMicroUSD = &zero
	if err := ValidateSummary(tampered); ExitCode(err) != 5 {
		t.Fatalf("ValidateSummary must reject zero cost when attempt cost is unavailable, got %v", err)
	}
}

func TestMustFailUnauthorizedOrRelabeledPublishAndBridgeProjectionFixture(t *testing.T) {
	raw, err := os.ReadFile("../../../testdata/workerbridge/codec.json")
	if err != nil {
		t.Fatal(err)
	}
	var golden map[string]json.RawMessage
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatal(err)
	}

	// Must-FAIL: relabeling surface as jules or chatgpt must be refused with rc 5.
	for _, relabeled := range []string{"jules", "chatgpt", "chatgpt-mcp", "chatgpt-app", "gemini-spark-mcp"} {
		if _, err := ProjectBridgeFixture(golden["worker"], golden["receipt"], "PROMPT-ID synthetic", relabeled); ExitCode(err) != 5 {
			t.Fatalf("expected rc 5 when relabeling surface to %q, got %v (%d)", relabeled, err, ExitCode(err))
		}
	}

	// Valid fixture projection preserves all 9 shared IDs + role and sets surface = "os-capsule".
	env, err := ProjectBridgeFixture(golden["worker"], golden["receipt"], "PROMPT-ID synthetic", OSSurface)
	if err != nil {
		t.Fatalf("ProjectBridgeFixture failed: %v", err)
	}
	if env.Mode != "fixture" || env.Result["surface"] != OSSurface {
		t.Fatalf("unexpected fixture envelope: mode=%q surface=%v", env.Mode, env.Result["surface"])
	}
	bridgeDoc, err := workerbridge.Parse(golden["bridge"])
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range append(workerbridge.SharedIDs(), "role") {
		if env.Result[key] != bridgeDoc[key] {
			t.Fatalf("shared identifier %s changed: got %v, want %v", key, env.Result[key], bridgeDoc[key])
		}
	}

	// Local only: live publication waits for C3 and canonical os-capsule surface, so Publish returns rc 4 unavailable.
	if _, err := Publish(env); ExitCode(err) != 4 {
		t.Fatalf("expected Publish to return rc 4 unavailable, got %v (%d)", err, ExitCode(err))
	}

	// Must-FAIL: publishing a relabeled or unauthorized envelope fails with rc 5.
	relabeledEnv := workerbridge.Envelope{
		Mode:   "live",
		Result: workerbridge.Document{"surface": "jules"},
	}
	if _, err := Publish(relabeledEnv); ExitCode(err) != 5 {
		t.Fatalf("expected rc 5 for relabeled publish, got %v (%d)", err, ExitCode(err))
	}
	unauthorizedEnv := workerbridge.Envelope{
		Mode:   "live",
		Result: workerbridge.Document{"surface": OSSurface},
	}
	if _, err := Publish(unauthorizedEnv); ExitCode(err) != 5 {
		t.Fatalf("expected rc 5 for unauthorized publish missing identifiers, got %v (%d)", err, ExitCode(err))
	}
}

func TestPositivePermutationsRedeliveryAndCRDTLaws(t *testing.T) {
	recA := sampleReproRecord()
	recA.Assertion = "FAIL: Alpha"
	recA.Affected = []string{"src/alpha.go"}
	recA.Frames[0].Symbol = "Alpha"

	recB := sampleReproRecord()
	recB.Assertion = "FAIL: Beta"
	recB.Affected = []string{"src/beta.go"}
	recB.Frames[0].Symbol = "Beta"

	idA, err := ComputeIdentity(recA, nil)
	if err != nil {
		t.Fatal(err)
	}

	segment := []byte("FAIL: Alpha\nFAIL: Beta\n")
	segSHA := DefaultHash(segment)

	makeObs := func(seq int64, rec ReproRecord, attempt string, cost int64, start, finish int64) Observation {
		r := rec
		att := AttemptReceiptRef{
			TaskID:        "task-" + attempt,
			AttemptID:     attempt,
			LeaseID:       "lease-" + attempt,
			ReceiptSHA256: digestHex("receipt-" + attempt),
			ExitCode:      5,
			FailureReason: "gate",
			StartedAtMS:   int64Ptr(start),
			FinishedAtMS:  int64Ptr(finish),
			CostMicroUSD:  int64Ptr(cost),
		}
		return Observation{
			WaveID:                "wave-perm",
			StreamKey:             "worker-stream",
			Seq:                   seq,
			BodyRef:               digestHex("body-" + attempt),
			Identity:              &r,
			Provenance:            Provenance{TaskID: att.TaskID, AttemptID: attempt, LeaseID: att.LeaseID, WorkerID: "w-" + attempt, Sender: "worker-stream", Pulse: seq},
			EvidenceSegment:       segment,
			EvidenceSegmentSHA256: segSHA,
			QuotedAnchors:         []ByteAnchor{{Quote: rec.Assertion}},
			AttemptReceipt:        &att,
		}
	}

	items := []Observation{
		makeObs(1, recA, "a1", 10000, 100, 200),
		makeObs(2, recB, "a2", 15000, 150, 300),
		makeObs(3, recA, "a3", 20000, 250, 450),
		makeObs(4, recB, "a4", 25000, 300, 600),
	}

	// Verify CRDT laws (idempotence, commutativity, associativity) on Manifestation merges.
	m1 := Manifestation{
		ReproSHA256:     idA.ReproSHA256,
		CanonicalRecord: idA.CanonicalRecord,
		FramedBytes:     idA.FramedBytes,
		Record:          recA,
		Observations:    []Observation{items[0]},
		AttemptReceipts: []AttemptReceiptRef{*items[0].AttemptReceipt},
		AffectedTargets: []AffectedTarget{{Path: "src/alpha.go", Symbol: "Alpha"}},
	}
	m2 := Manifestation{
		ReproSHA256:     idA.ReproSHA256,
		CanonicalRecord: idA.CanonicalRecord,
		FramedBytes:     idA.FramedBytes,
		Record:          recA,
		Observations:    []Observation{items[2]},
		AttemptReceipts: []AttemptReceiptRef{*items[2].AttemptReceipt},
		AffectedTargets: []AffectedTarget{{Path: "src/alpha_helper.go", Symbol: "AlphaHelper"}},
	}
	obsA3 := makeObs(5, recA, "a5", 30000, 400, 700)
	m3 := Manifestation{
		ReproSHA256:     idA.ReproSHA256,
		CanonicalRecord: idA.CanonicalRecord,
		FramedBytes:     idA.FramedBytes,
		Record:          recA,
		Observations:    []Observation{obsA3},
		AttemptReceipts: []AttemptReceiptRef{*obsA3.AttemptReceipt},
		AffectedTargets: []AffectedTarget{{Path: "src/alpha_extra.go", Symbol: "AlphaExtra"}},
	}

	idem, err := MergeManifestations(m1, m1)
	if err != nil {
		t.Fatal(err)
	}
	bM1, _ := json.Marshal(m1)
	bIdem, _ := json.Marshal(idem)
	if !bytes.Equal(bM1, bIdem) {
		t.Fatal("MergeManifestations violated idempotence")
	}

	m12, err := MergeManifestations(m1, m2)
	if err != nil {
		t.Fatal(err)
	}
	m21, err := MergeManifestations(m2, m1)
	if err != nil {
		t.Fatal(err)
	}
	b12, _ := json.Marshal(m12)
	b21, _ := json.Marshal(m21)
	if !bytes.Equal(b12, b21) {
		t.Fatal("MergeManifestations violated commutativity")
	}

	leftAssoc, err := MergeManifestations(m12, m3)
	if err != nil {
		t.Fatal(err)
	}
	m23, err := MergeManifestations(m2, m3)
	if err != nil {
		t.Fatal(err)
	}
	rightAssoc, err := MergeManifestations(m1, m23)
	if err != nil {
		t.Fatal(err)
	}
	bLeft, _ := json.Marshal(leftAssoc)
	bRight, _ := json.Marshal(rightAssoc)
	if !bytes.Equal(bLeft, bRight) {
		t.Fatal("MergeManifestations violated associativity")
	}

	permutations := [][]int{
		{0, 1, 2, 3},
		{3, 2, 1, 0},
		{2, 0, 3, 1, 0, 2, 1, 3}, // out-of-order with redeliveries
		{1, 3, 0, 2, 3, 1},       // interleaved with redeliveries
	}

	var baselineJSON []byte
	for pIdx, order := range permutations {
		agg, err := NewAggregator(Config{WaveID: "wave-perm", BaseSeq: 0})
		if err != nil {
			t.Fatal(err)
		}
		for _, idx := range order {
			if err := agg.Ingest(items[idx]); err != nil {
				t.Fatalf("perm %d item %d failed: %v", pIdx, idx, err)
			}
		}
		sum := agg.Summary()
		if sum.UniqueManifestationCount != 2 {
			t.Fatalf("perm %d: UniqueManifestationCount=%d, want 2", pIdx, sum.UniqueManifestationCount)
		}
		if len(sum.MissingIntervals) != 0 {
			t.Fatalf("perm %d: unexpected missing intervals %+v", pIdx, sum.MissingIntervals)
		}
		if sum.TotalCostMicroUSD == nil || *sum.TotalCostMicroUSD != 70000 {
			t.Fatalf("perm %d: TotalCostMicroUSD=%v, want 70000", pIdx, sum.TotalCostMicroUSD)
		}
		if sum.WallTimeMS == nil || *sum.WallTimeMS != 500 {
			t.Fatalf("perm %d: WallTimeMS=%v, want 500", pIdx, sum.WallTimeMS)
		}
		for _, item := range items {
			if !agg.Acknowledged(item.BodyRef) {
				t.Fatalf("perm %d: expected body_ref %s to be acknowledged", pIdx, item.BodyRef)
			}
		}
		encoded, err := json.Marshal(sum)
		if err != nil {
			t.Fatal(err)
		}
		if pIdx == 0 {
			baselineJSON = encoded
		} else if !bytes.Equal(baselineJSON, encoded) {
			t.Fatalf("perm %d summary differs from baseline:\ngot:  %s\nwant: %s", pIdx, encoded, baselineJSON)
		}
	}
}

func TestExitCodesNormalizedNeverOne(t *testing.T) {
	cases := []struct {
		err  error
		want int
	}{
		{nil, 0},
		{errors.New("unclassified"), 5},
		{&workerbridge.Reject{Code: 1, Class: "bad"}, 5},
		{&workerbridge.Reject{Code: 2, Class: "input"}, 2},
		{&workerbridge.Reject{Code: 3, Class: "exec"}, 3},
		{&workerbridge.Reject{Code: 4, Class: "unavailable"}, 4},
		{&workerbridge.Reject{Code: 5, Class: "gate"}, 5},
		{&workerbridge.Reject{Code: 6, Class: "indeterminate"}, 6},
		{&workerbridge.Reject{Code: 7, Class: "operator"}, 7},
	}
	for _, tc := range cases {
		got := ExitCode(tc.err)
		if got == 1 || got != tc.want {
			t.Fatalf("ExitCode(%v) = %d, want %d (never 1)", tc.err, got, tc.want)
		}
	}
}
