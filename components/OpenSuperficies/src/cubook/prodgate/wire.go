package prodgate

import (
	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/rc"

	"opensuperficies.local/srf/src/displaybook/desktop"
)

// XIso is the real IsoReader: a root-window GetProperty over the capsule X server.
type XIso struct{}

// Read implements IsoReader.
func (XIso) Read(display string) (string, error) {
	_, c, err := capture.DialX(display)
	if err != nil {
		return "", err
	}
	defer c.Close()
	return capture.ReadIso(c)
}

// Args are the production inputs of a CU binary.
type Args struct {
	FB, RamwsDir string
	Deny         *exec.DenyList
	Cfg          exec.Config
}

// Deps are the effects behind Build; tests inject fakes, Real() is production.
type Deps struct {
	Env   func(string) string
	Probe capture.CapsuleProbe
	Iso   IsoReader
	// XTest opens the XTEST client; NewObserver opens the Snapper observer.
	XTest       func(display string) (exec.XTestAPI, func(), error)
	NewObserver func(fb, ramDir, display string) (exec.Observer, func(), error)
}

// Real is the production dependency set.
func Real(env func(string) string) Deps {
	return Deps{
		Env: env, Probe: capture.HostProbe(), Iso: XIso{},
		XTest: func(display string) (exec.XTestAPI, func(), error) {
			c, err := desktop.NewXTestClient(display)
			if err != nil {
				return nil, nil, rc.New("E_IO", err.Error())
			}
			return c, func() { c.Close() }, nil
		},
		NewObserver: func(fb, ramDir, display string) (exec.Observer, func(), error) {
			o, err := capture.NewSnapObserver(fb, ramDir, display)
			if err != nil {
				return nil, nil, err
			}
			return o, o.Close, nil
		},
	}
}

// Build is the single production door: Check first (zero events on refusal),
// then XTestActuator + Snapper observer. The returned func releases everything.
func Build(d Deps, a Args) (*exec.Executor, func(), error) {
	m, err := Check(d.Env, d.Probe, d.Iso)
	if err != nil {
		return nil, nil, err
	}
	if m != Prod {
		return nil, nil, rc.New("E_GATE", "not_prod")
	}
	if a.FB == "" || a.RamwsDir == "" {
		return nil, nil, rc.New("E_USAGE", "prod needs --fb and --ramws-dir")
	}
	x, closeX, err := d.XTest(":100")
	if err != nil {
		return nil, nil, err
	}
	obs, closeObs, err := d.NewObserver(a.FB, a.RamwsDir, ":100")
	if err != nil {
		closeX()
		return nil, nil, err
	}
	cfg := a.Cfg
	cfg.Deny = a.Deny
	act := &exec.XTestActuator{X: x}
	e := exec.New(cfg, act, obs, exec.XYLocator{}, nil)
	return e, func() { act.ReleaseAll(); closeObs(); closeX() }, nil
}
