package action

import (
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/rc"
)

const clickJSON = `{"schema":"vqa.action/v1","step":1,"op":"click","target":{"by":"xy","xy":[10,20]},"args":{"button":1,"count":1},"pre":{"match_min":"L1"},"expect":[{"kind":"changed_roi","roi":"target"}],"risk":"read","allow":null,"fallback":"none","on_fail":"stop","policy":"script","reason_sha8":"deadbeef"}`

func wantSchema(t *testing.T, js string) {
	t.Helper()
	_, err := DecodeAction([]byte(js))
	if err == nil || rc.Of(err) != 2 {
		t.Fatalf("want E_SCHEMA rc 2, got %v", err)
	}
}

func TestActionV1Accepts(t *testing.T) {
	if _, err := DecodeAction([]byte(clickJSON)); err != nil {
		t.Fatal(err)
	}
}

func TestActionV1RejectsFloat(t *testing.T) {
	wantSchema(t, strings.Replace(clickJSON, `"step":1`, `"step":1.0`, 1))
	wantSchema(t, strings.Replace(clickJSON, `[10,20]`, `[10,2e1]`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"count":1`, `"count":1.5`, 1))
}

func TestActionV1RejectsUnknownEnum(t *testing.T) {
	wantSchema(t, strings.Replace(clickJSON, `"op":"click"`, `"op":"teleport"`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"risk":"read"`, `"risk":"yolo"`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"L1"`, `"L9"`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"button":1`, `"button":4`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"count":1`, `"count":3`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"step":1,`, `"step":1,"bogus":1,`, 1))
	wantSchema(t, strings.Replace(clickJSON, `"kind":"changed_roi"`, `"kind":"magic"`, 1))
}

func TestActionV1NoTextLiteral(t *testing.T) {
	typ := `{"schema":"vqa.action/v1","step":2,"op":"type","args":{"text":"hunter2"},"pre":{"match_min":"L1"},"expect":[],"risk":"read","allow":null,"fallback":"none","on_fail":"stop","policy":"script","reason_sha8":"deadbeef"}`
	wantSchema(t, typ)
	wantSchema(t, strings.Replace(typ, `"text":"hunter2"`, `"text_ref":"t1"`, 1)) // needs sha+len
	ok := strings.Replace(typ, `"text":"hunter2"`, `"text_ref":"t1","text_sha256":"`+strings.Repeat("a", 64)+`","len":7`, 1)
	if _, err := DecodeAction([]byte(ok)); err != nil {
		t.Fatal(err)
	}
}

func TestActionShaCanonical(t *testing.T) {
	a, err := DecodeAction([]byte(clickJSON))
	if err != nil {
		t.Fatal(err)
	}
	reordered := `{"reason_sha8":"deadbeef","policy":"script","on_fail":"stop","fallback":"none","allow":null,"risk":"read","expect":[{"roi":"target","kind":"changed_roi"}],"pre":{"match_min":"L1"},"args":{"count":1,"button":1},"target":{"xy":[10,20],"by":"xy"},"op":"click","step":1,"schema":"vqa.action/v1"}`
	b, err := DecodeAction([]byte(reordered))
	if err != nil {
		t.Fatal(err)
	}
	sa, _ := a.SHA()
	sb, _ := b.SHA()
	if sa != sb || len(sa) != 64 {
		t.Fatalf("sha not canonical: %s vs %s", sa, sb)
	}
	b.Step = 2
	sc, _ := b.SHA()
	if sc == sa {
		t.Fatal("sha ignores content")
	}
}
