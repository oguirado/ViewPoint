package main

import (
	"bytes"
	"context"
	"opensuperficies.local/srf/src/w8seat"
	"strings"
	"testing"
)

func TestCLIOnlyNormalizedCodes(t *testing.T) {
	for _, c := range []struct {
		args []string
		rc   int
	}{{[]string{"--unknown"}, 2}, {[]string{"submit"}, 2}, {[]string{"--profile-sha"}, 0}} {
		var out, diag bytes.Buffer
		if rc := run(context.Background(), c.args, &out, &diag); rc != c.rc {
			t.Fatalf("%v rc%d %s", c.args, rc, diag.String())
		}
		if c.rc == 0 && strings.TrimSpace(out.String()) != w8seat.ProfileSHA() {
			t.Fatal(out.String())
		}
	}
}
