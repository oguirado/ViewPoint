//go:build cockpit

package main

import (
	"errors"
	"sync"
	"testing"
)

// The srfd source deliberately has no custody fields. Only native cockpit hooks
// may supply them; the fake Stop models srfd's successful stop/list response.
func nativeCustodyFixture(t *testing.T) (*App, *stateInput) {
	t.Helper()
	a, _, in := cooperationFixture(t)
	StopCooperation(a)
	cfg := nativeCooperationConfig(a)
	cfg.Clip = func(string) error { return nil }
	cfg.Stop = func(id string) error {
		for i := range in.Capsules {
			if cooperationID(in.Capsules[i].Slot) == id {
				in.Capsules[i].Lifecycle = "stopped"
			}
		}
		return nil
	}
	if err := StartCooperation(a, cfg); err != nil {
		t.Fatal(err)
	}
	return a, in
}

func custodyLaunch(t *testing.T, a *App, in *stateInput) LeaseCredential {
	t.Helper()
	got, err := launchGuard(a, "desktop", 3, "launching-ai", func(string, string, uint16) (*RespuestaSrfd, error) {
		in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: 3, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"})
		if a.EstadoCanonico() != "" {
			t.Error("launch visible before custody grant")
		}
		return &RespuestaSrfd{OK: true}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.Lease == nil || got.Lease.Holder != "launching-ai" {
		t.Fatal("missing launching holder")
	}
	return *got.Lease
}

func TestCustodyNativeLaunchAndTakeover(t *testing.T) {
	a, in := nativeCustodyFixture(t)
	cred := custodyLaunch(t, a, in)
	c := cooperationFor(a)
	s, err := c.slot("srf-ng-3", cred.Epoch)
	if err != nil || s.Mode != "agent_locked" || s.LeaseHolder == nil || *s.LeaseHolder != cred.Holder {
		t.Fatalf("custody projection: %+v %v", s, err)
	}
	if _, err = a.CopiarSlot(s.SlotID, CopyRequest{Kind: "bloque", Credential: &cred}); err != nil {
		t.Fatal(err)
	}
	foreign := cred
	foreign.Holder = "second-ai"
	if c.authenticate(s, &foreign) == nil {
		t.Fatal("foreign AI drove slot")
	}
	if err = a.CerrarSlot(s.SlotID, SlotAuthority{Epoch: cred.Epoch, Credential: &foreign}); err == nil {
		t.Fatal("foreign AI closed slot")
	}
	if err = a.TomarControl(s.SlotID, cred.Epoch); err != nil {
		t.Fatal(err)
	}
	d, err := c.document()
	if err != nil {
		t.Fatal(err)
	}
	var after StateCapsule
	for _, v := range d.Capsules {
		if v.SlotID == s.SlotID {
			after = v
		}
	}
	if after.Mode != "operator_controlled" || after.LeaseHolder != nil || after.LeaseEpoch <= cred.Epoch {
		t.Fatalf("takeover: %+v", after)
	}
	if c.authenticate(after, &cred) == nil {
		t.Fatal("old credential survived takeover")
	}
	if err = a.CederControl(s.SlotID, after.LeaseEpoch); err != nil {
		t.Fatal(err)
	}
	next, err := a.SolicitarArriendo(s.SlotID, "next-ai", after.LeaseEpoch)
	if err != nil || next.Epoch <= after.LeaseEpoch {
		t.Fatalf("regrant: %+v %v", next, err)
	}
	if err = a.CerrarSlot(s.SlotID, SlotAuthority{Epoch: next.Epoch, Credential: &next}); err != nil {
		t.Fatal(err)
	}
	for _, v := range in.Capsules {
		if v.Slot == 3 && v.LeaseHolder != nil {
			t.Fatal("source unexpectedly mutated")
		}
	}
}

func TestCustodyStoppedMissingAndRestart(t *testing.T) {
	for _, action := range []string{"stopped", "missing", "restart"} {
		t.Run(action, func(t *testing.T) {
			a, in := nativeCustodyFixture(t)
			cred := custodyLaunch(t, a, in)
			old := cooperationFor(a)
			s, err := old.slot("srf-ng-3", cred.Epoch)
			if err != nil {
				t.Fatal(err)
			}
			switch action {
			case "stopped":
				in.Capsules[len(in.Capsules)-1].Lifecycle = "stopped"
			case "missing":
				in.Capsules = in.Capsules[:len(in.Capsules)-1]
			case "restart":
				StopCooperation(a)
				if err := StartCooperation(a, nativeCooperationConfig(a)); err != nil {
					t.Fatal(err)
				}
			}
			c := cooperationFor(a)
			if _, err := c.document(); err != nil {
				t.Fatal(err)
			}
			if c.authenticate(s, &cred) == nil {
				t.Fatal("stale credential survived custody removal")
			}
			if action != "restart" {
				if action == "missing" {
					in.Capsules = append(in.Capsules, stateCapsuleInput{Slot: 3, Archetype: "desktop", Lifecycle: "ready", Health: "healthy", Mode: "operator_controlled"})
				} else {
					in.Capsules[len(in.Capsules)-1].Lifecycle = "ready"
				}
			}
			d, err := c.document()
			if err != nil {
				t.Fatal(err)
			}
			for _, v := range d.Capsules {
				if v.SlotID == "srf-ng-3" && (v.LeaseHolder != nil || v.Mode == "agent_locked") {
					t.Fatal("stale custody resurrected")
				}
			}
		})
	}
}

func TestCustodyConcurrentProjection(t *testing.T) {
	a, _ := nativeCustodyFixture(t)
	c := cooperationFor(a)
	var wg sync.WaitGroup
	for worker := range 4 {
		wg.Go(func() {
			for range 16 {
				if worker%2 == 0 {
					if err := c.config.SetMode("srf-ng-1", "read_only"); err != nil {
						t.Error(err)
						return
					}
					if err := c.config.SetMode("srf-ng-1", "operator_controlled"); err != nil {
						t.Error(err)
						return
					}
				} else if _, err := c.document(); err != nil {
					t.Error(err)
					return
				}
			}
		})
	}
	wg.Wait()
}

func TestCustodyJournalFailureDoesNotPublish(t *testing.T) {
	a, _ := nativeCustodyFixture(t)
	c := cooperationFor(a)
	if err := c.config.SetMode("srf-ng-1", "read_only"); err != nil {
		t.Fatal(err)
	}
	c.custody.journal = func(string, string, slotCustody) error { return errors.New("journal unavailable") }
	if err := c.config.Grant("srf-ng-1", "agent", 1); err == nil {
		t.Fatal("unjournaled grant admitted")
	}
	d, err := c.document()
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range d.Capsules {
		if v.SlotID == "srf-ng-1" && (v.Mode != "read_only" || v.LeaseHolder != nil) {
			t.Fatal("unjournaled custody published")
		}
	}
}
