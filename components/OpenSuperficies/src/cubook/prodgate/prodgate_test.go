package prodgate

import (
	"errors"
	"testing"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/rc"
)

const goodIso = "0123456789abcdef0123456789abcdef"

type fakeIso struct {
	v     string
	err   error
	calls int
}

func (f *fakeIso) Read(string) (string, error) { f.calls++; return f.v, f.err }

func envOf(m map[string]string, seen *[]string) func(string) string {
	return func(k string) string {
		if seen != nil {
			*seen = append(*seen, k)
		}
		return m[k]
	}
}

func capsule(yes bool) capture.CapsuleProbe {
	return capture.CapsuleProbe{Exists: func(p string) bool { return yes && p == "/.dockerenv" }}
}

func wantGate(t *testing.T, err error, name string) {
	t.Helper()
	if err == nil {
		t.Fatalf("want %s refusal, got nil", name)
	}
	if rc.Of(err) != 5 {
		t.Fatalf("rc %d, want 5: %v", rc.Of(err), err)
	}
	if e := err.Error(); !contains(e, name) {
		t.Fatalf("error %q lacks %q", e, name)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func prodEnv() map[string]string {
	return map[string]string{"DISPLAY": ":100", "VQA_ISO": goodIso}
}

func TestProdGateRefusesHostNoMarkers(t *testing.T) {
	iso := &fakeIso{v: goodIso}
	m, err := Check(envOf(prodEnv(), nil), capsule(false), iso)
	wantGate(t, err, "not_in_capsule")
	if m != 0 || iso.calls != 0 {
		t.Fatalf("mode %v, iso reads %d (must be 0 before the capsule test)", m, iso.calls)
	}
}

func TestProdGateRefusesIsoMismatch(t *testing.T) {
	_, err := Check(envOf(prodEnv(), nil), capsule(true), &fakeIso{v: "ffffffffffffffffffffffffffffffff"})
	wantGate(t, err, "iso_mismatch")
	_, err = Check(envOf(prodEnv(), nil), capsule(true), &fakeIso{err: errors.New("x")})
	wantGate(t, err, "iso_mismatch")
	e := prodEnv()
	e["VQA_ISO"] = "short"
	_, err = Check(envOf(e, nil), capsule(true), &fakeIso{v: "short"})
	wantGate(t, err, "iso_mismatch")
}

func TestProdGateRefusesDisplayNot100(t *testing.T) {
	for _, d := range []string{"", ":0", ":101", "host:100"} {
		e := prodEnv()
		e["DISPLAY"] = d
		iso := &fakeIso{v: goodIso}
		_, err := Check(envOf(e, nil), capsule(true), iso)
		wantGate(t, err, "host_display")
		if iso.calls != 0 {
			t.Fatalf("display %q: iso read before display check", d)
		}
	}
}

func TestProdGateTestFakeOnlyWithEnv(t *testing.T) {
	e := prodEnv()
	e["OS_CU_TEST"] = "1"
	m, err := Check(envOf(e, nil), capsule(false), &fakeIso{})
	if err != nil || m != TestFake {
		t.Fatalf("OS_CU_TEST=1 -> %v %v", m, err)
	}
	for _, v := range []string{"", "0", "true", "yes"} {
		e["OS_CU_TEST"] = v
		m, err := Check(envOf(e, nil), capsule(false), &fakeIso{})
		if err == nil || m == TestFake {
			t.Fatalf("OS_CU_TEST=%q must not give TestFake (%v)", v, m)
		}
	}
	m, err = Check(envOf(prodEnv(), nil), capsule(true), &fakeIso{v: goodIso})
	if err != nil || m != Prod {
		t.Fatalf("full prod conditions -> %v %v", m, err)
	}
}

func TestNoDisplayEnvReadInFakeMode(t *testing.T) {
	var seen []string
	iso := &fakeIso{v: goodIso}
	m, err := Check(envOf(map[string]string{"OS_CU_TEST": "1", "DISPLAY": ":0"}, &seen), capsule(true), iso)
	if err != nil || m != TestFake {
		t.Fatalf("%v %v", m, err)
	}
	for _, k := range seen {
		if k == "DISPLAY" || k == "VQA_ISO" {
			t.Fatalf("fake mode read %s", k)
		}
	}
	if iso.calls != 0 {
		t.Fatal("fake mode touched the iso reader")
	}
}

func TestNoRc1(t *testing.T) {
	for _, c := range []struct {
		e  map[string]string
		in bool
	}{{nil, false}, {prodEnv(), false}, {map[string]string{"DISPLAY": ":9"}, true}} {
		_, err := Check(envOf(c.e, nil), capsule(c.in), &fakeIso{})
		if r := rc.Of(err); r == 1 {
			t.Fatal("rc 1")
		}
	}
}

// JW1-05: Build is defence in depth: OS_CU_TEST=1 (TestFake) never reaches the real XTEST.
func TestBuildRefusesTestFake(t *testing.T) {
	xt, obs := 0, 0
	d := Deps{
		Env: envOf(map[string]string{"OS_CU_TEST": "1", "DISPLAY": ":100", "VQA_ISO": goodIso}, nil), Probe: capsule(true), Iso: &fakeIso{v: goodIso},
		XTest:       func(string) (exec.XTestAPI, func(), error) { xt++; return nil, func() {}, nil },
		NewObserver: func(string, string, string) (exec.Observer, func(), error) { obs++; return nil, func() {}, nil },
	}
	e, _, err := Build(d, Args{FB: "a", RamwsDir: "b"})
	wantGate(t, err, "not_prod")
	if e != nil || xt != 0 || obs != 0 {
		t.Fatalf("TestFake reached effects: xtest=%d observer=%d", xt, obs)
	}
}
