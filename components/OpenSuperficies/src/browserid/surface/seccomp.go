package surface

import (
	"bytes"
	_ "embed"
	"os"
	"path/filepath"
)

// identitySeccomp is Docker's default seccomp profile (moby/profiles seccomp/default.json, sha256
// 6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6, fetched 2026-10-23 with the
// operator's approval) plus ONE rule allowing clone/unshare/setns/chroot without CAP_SYS_ADMIN:
// Chromium's namespace sandbox needs them, and the operator chose this over --no-sandbox.
//
//go:embed identity-seccomp.json
var identitySeccomp []byte

// writeSeccomp places the profile in the private state root (docker reads it client side).
func (t Tool) writeSeccomp() (string, error) {
	p := filepath.Join(t.root(), "identity-seccomp.json")
	if old, e := os.ReadFile(p); e == nil && bytes.Equal(old, identitySeccomp) {
		return p, nil
	}
	tmp := p + ".tmp"
	if e := os.WriteFile(tmp, identitySeccomp, 0o600); e != nil {
		return "", fail(4, "seccomp profile unavailable")
	}
	if e := os.Rename(tmp, p); e != nil {
		return "", fail(4, "seccomp profile unavailable")
	}
	return p, nil
}
