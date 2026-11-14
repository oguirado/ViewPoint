package w8builder

import (
	"encoding/json"
	"testing"
)

func TestRecordFrozenContractAndBindings(t *testing.T) {
	r := newRecord(digest([]byte("recipe fixture")), digest([]byte("image fixture")), digest([]byte("approval fixture")))
	b, _ := json.Marshal(r)
	if e := CheckRecord(b); e != nil {
		t.Fatal(e)
	}
	for k, v := range map[string]any{"repo_code_in_image": 1, "frame_bytes_in_image": 1, "user_uid": 0, "retained_rank": 3, "unknown": true} {
		copy := newRecord(digest([]byte("recipe fixture")), digest([]byte("image fixture")), digest([]byte("approval fixture")))
		copy[k] = v
		b, _ := json.Marshal(copy)
		if ExitCode(CheckRecord(b)) != 5 {
			t.Fatal(k)
		}
	}
	r["approval"].(map[string]any)["recipe_sha256"] = digest([]byte("different recipe"))
	b, _ = json.Marshal(r)
	if ExitCode(CheckRecord(b)) != 5 {
		t.Fatal("approval recipe mismatch")
	}
}

func TestRecordRejectsMalformedIdentityEvidence(t *testing.T) {
	for _, evidence := range []any{nil, []any{}, map[string]any{"size": 0, "sha256": digest([]byte("set"))}, map[string]any{"size": 1, "sha256": "bad"}, map[string]any{"size": 1.5, "sha256": digest([]byte("set"))}, map[string]any{"size": 1, "sha256": digest([]byte("set")), "extra": true}} {
		rec := newRecord(digest([]byte("recipe")), digest([]byte("image")), digest([]byte("approval")))
		rec["repo_identity"] = evidence
		b, _ := json.Marshal(rec)
		if ExitCode(CheckRecord(b)) != 5 {
			t.Fatal("invalid identity evidence accepted", evidence)
		}
	}
}
