package capture

import "testing"

// JW4-05: an entropy failure must never produce a silently unfenced run: Run refuses (E_GATE run_nonce) instead of
// continuing with an empty nonce.
func TestRunEmptyNonceRefused(t *testing.T) {
	old := NewRunNonce
	NewRunNonce = func() string { return "" }
	t.Cleanup(func() { NewRunNonce = old })
	r := newRig(t)
	r.cfg.WriteStart = true
	r.cfg.RunNonce = ""
	_, err := Run(r.cfg)
	if c, rs := reason(err); c != ClassGate || rs != "run_nonce" {
		t.Fatalf("empty nonce: class=%q reason=%q err=%v", c, rs, err)
	}
}
