//go:build cockpit

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"syscall"

	"opensuperficies.local/srf/src/browserid/services"
	"opensuperficies.local/srf/src/surfacebook/manager"
)

// serviciosCatalog is the temporary egress catalog adapter. The operator supplies
// <directory containing srfd.sock>/services/catalog.json: a JSON array of unique
// approved service names, e.g. ["chatgpt","perplexity"]. Missing/invalid input
// fails closed. It contains no domain bundles and never approves new services.
// Each binding reads an immutable snapshot; the next call observes replacements.
type serviciosCatalog []string

func (c serviciosCatalog) HasService(name string) bool { return slices.Contains(c, name) }
func (c serviciosCatalog) Services() []string          { return slices.Clone(c) }

func serviciosDir() string { return filepath.Join(filepath.Dir(manager.RutaSocket("")), "services") }
func loadServiciosCatalog(dir string) (serviciosCatalog, error) {
	fd, err := syscall.Open(filepath.Join(dir, "catalog.json"), syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), "catalog.json")
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !st.Mode().IsRegular() {
		return nil, errors.New("REFUSED: invalid services catalog file")
	}
	const limit = 1024 * 1024
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if len(b) > limit {
		return nil, errors.New("REFUSED: services catalog too large")
	}
	var names []string
	d := json.NewDecoder(bytes.NewReader(b))
	if err = d.Decode(&names); err != nil {
		return nil, err
	}
	if names == nil || d.Decode(new(any)) != io.EOF {
		return nil, services.ErrInvalid
	}
	valid := regexp.MustCompile(`^[a-z][a-z0-9_-]{1,31}$`)
	seen := map[string]bool{}
	for _, name := range names {
		if !valid.MatchString(name) || seen[name] {
			return nil, services.ErrInvalid
		}
		seen[name] = true
	}
	slices.Sort(names)
	return serviciosCatalog(names), nil
}

// ServiciosSnapshot is separate from the frozen cockpit-state/v1 document.
// Capsules includes current cockpit capsules, assigned capsules, and request
// targets. Empty historical registry capsules have no public enumeration API.
type ServiciosSnapshot struct {
	Services    []string            `json:"services"`
	Capsules    []string            `json:"capsules"`
	Assignments map[string][]string `json:"assignments"`
	Pending     []services.Request  `json:"pending"`
	Revision    uint64              `json:"revision"`
}

func (a *App) ServiciosEstado() (ServiciosSnapshot, error) {
	c := cooperationFor(a)
	if c == nil {
		return ServiciosSnapshot{}, cooperationMissing()
	}
	doc, err := c.document()
	if err != nil {
		return ServiciosSnapshot{}, err
	}
	cat, err := loadServiciosCatalog(serviciosDir())
	if err != nil {
		return ServiciosSnapshot{}, err
	}
	r, err := services.Open(serviciosDir(), cat)
	if err != nil {
		return ServiciosSnapshot{}, err
	}
	// Public queries each acquire their own cross-process lock. Bound retries and
	// compare revisions so concurrent srfd requests cannot produce a mixed matrix.
	for range 3 {
		before, err := r.Status("capsule-0")
		if err != nil {
			return ServiciosSnapshot{}, err
		}
		s := ServiciosSnapshot{Services: cat.Services(), Capsules: []string{}, Assignments: map[string][]string{}, Revision: before.Revision}
		known := map[string]bool{}
		for _, capsule := range doc.Capsules {
			known[capsule.SlotID] = true
		}
		for _, name := range s.Services {
			q, err := r.WhereCanOpen(name)
			if err != nil {
				return ServiciosSnapshot{}, err
			}
			for _, id := range q.AvailableIn {
				known[id] = true
			}
		}
		s.Pending, err = r.ListPending()
		if err != nil {
			return ServiciosSnapshot{}, err
		}
		for _, req := range s.Pending {
			known[req.Capsule] = true
		}
		for id := range known {
			v, err := r.Status(id)
			if err != nil {
				return ServiciosSnapshot{}, err
			}
			s.Capsules = append(s.Capsules, id)
			s.Assignments[id] = v.Services
		}
		after, err := r.Status("capsule-0")
		if err != nil {
			return ServiciosSnapshot{}, err
		}
		if before.Revision == after.Revision {
			slices.Sort(s.Capsules)
			return s, nil
		}
	}
	return ServiciosSnapshot{}, errors.New("REFUSED: services registry changed during read; refresh")
}

// This credential is private to the native embedding host. No constructor,
// registry, or OperatorToken is an App method or returned to the webview. Each
// write passes exactly once through the same WithOperatorGate/intent WAL seam
// as start-capsule, before OpenOperator can mint its instance-bound capability.
type serviciosCredential struct {
	app          *App
	kind, target string
}

func (c serviciosCredential) AuthorizeServices() error {
	return AuthorizeCooperationOperator(c.app, c.kind, c.target, 0)
}
func (a *App) serviciosWrite(kind, target string, apply func(*services.Registry, services.OperatorToken) error) error {
	cat, err := loadServiciosCatalog(serviciosDir())
	if err != nil {
		return err
	}
	r, token, err := services.OpenOperator(serviciosDir(), cat, serviciosCredential{a, kind, target})
	if err != nil {
		return err
	}
	return apply(r, token)
}
func (a *App) ServiciosAsignar(capsule, service string) error {
	return a.serviciosWrite("services-assign", capsule+"/"+service, func(r *services.Registry, t services.OperatorToken) error { return r.Assign(t, capsule, service) })
}
func (a *App) ServiciosDesasignar(capsule, service string) error {
	return a.serviciosWrite("services-unassign", capsule+"/"+service, func(r *services.Registry, t services.OperatorToken) error { return r.Unassign(t, capsule, service) })
}
func (a *App) ServiciosAprobar(requestID string) error {
	return a.serviciosWrite("services-approve", requestID, func(r *services.Registry, t services.OperatorToken) error { return r.Approve(t, requestID) })
}
func (a *App) ServiciosDenegar(requestID string) error {
	return a.serviciosWrite("services-deny", requestID, func(r *services.Registry, t services.OperatorToken) error { return r.Deny(t, requestID) })
}
