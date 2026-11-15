package w8seat

import _ "embed"

//go:embed w8-seccomp.json
var profile []byte

func ProfileSHA() string { return hash(profile) }
