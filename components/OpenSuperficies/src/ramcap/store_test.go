package ramcap

import (
	"testing"
)

func quotaStore() *Store {
	return &Store{budget: 100, opts: StoreOptions{CheckRunDir: func(string) error { return nil }, RunUsed: func() (int64, error) { return 0, nil }, HostUsed: func() (int64, error) { return 0, nil }, FreeBytes: func() (int64, error) { return FreeMin + 100, nil }}}
}
func TestStoreRefusesNonTmpfs(t *testing.T) {
	_, e := OpenStore(t.TempDir())
	if e == nil {
		t.Fatal("non-RAM accepted")
	}
}
func TestStoreEnforcesFreeMinAndLiveSwapLatch(t *testing.T) {
	for _, c := range []struct {
		name   string
		change func(*Store)
	}{
		{"session", func(s *Store) { s.used = 100 }},
		{"run", func(s *Store) { s.opts.RunUsed = func() (int64, error) { return RunMax + 1, nil } }},
		{"host", func(s *Store) { s.opts.HostUsed = func() (int64, error) { return HostMax + 1, nil } }},
		{"free", func(s *Store) { s.opts.FreeBytes = func() (int64, error) { return FreeMin - 1, nil } }},
	} {
		t.Run(c.name, func(t *testing.T) {
			s := quotaStore()
			c.change(s)
			old := s.used
			if e := s.reserve(1); e == nil {
				t.Fatal("quota accepted")
			}
			if s.used != old {
				t.Fatal("failed reservation mutated usage")
			}
		})
	}
	s := quotaStore()
	s.swap0 = 10
	n := int64(10)
	s.opts.SwapUsed = func() (int64, error) { return n, nil }
	v, e := s.Swap()
	if e != nil || v {
		t.Fatal(v, e)
	}
	n = 11
	v, e = s.Swap()
	if e != nil || !v {
		t.Fatal(v, e)
	}
	n = 0
	v, e = s.Swap()
	if e != nil || !v {
		t.Fatal("unlatched", v, e)
	}
}
func TestReserveBoundary(t *testing.T) {
	s := quotaStore()
	if e := s.reserve(100); e != nil {
		t.Fatal(e)
	}
	s.opts.FreeBytes = func() (int64, error) { return FreeMin + 99, nil }
	code(t, s.reserve(100), "E_RAM_QUOTA")
	code(t, s.reserve(-1), "E_BUDGET")
}
