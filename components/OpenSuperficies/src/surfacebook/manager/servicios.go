package manager

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sync"
	"time"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/browserid/services"
)

// serviciosDaemon owns only an AI-capable registry: it never opens or holds an
// OperatorToken. The cockpit is the sole operator-write host. This temporary
// catalog adapter reads <daemon socket directory>/services/catalog.json, an
// operator-maintained JSON array of unique approved service names (no domains).
// Each command reloads a bounded snapshot so removals take effect immediately.
// Missing/invalid catalog data fails closed without disabling unrelated verbs.
type serviciosDaemon struct {
	mu       sync.Mutex
	registry *services.Registry
}

type catalogoServicios map[string]bool

func (c catalogoServicios) HasService(name string) bool { return c[name] }
func (c catalogoServicios) Services() []string {
	names := make([]string, 0, len(c))
	for name := range c {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

var nombreServicio = regexp.MustCompile(`^[a-z][a-z0-9_-]{1,31}$`)

func leerCatalogoServicios(path string) (catalogoServicios, error) {
	// Refuse symlinks, devices, directories and FIFOs without blocking on open.
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("services: catalog must be a regular file")
	}
	const maxCatalog = 1024 * 1024
	raw, err := io.ReadAll(io.LimitReader(f, maxCatalog+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > maxCatalog {
		return nil, fmt.Errorf("services: catalog too large")
	}
	var names []string
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := dec.Decode(&names); err != nil {
		return nil, err
	}
	if names == nil || dec.Decode(new(any)) != io.EOF {
		return nil, fmt.Errorf("services: invalid catalog array")
	}
	c := catalogoServicios{}
	for _, name := range names {
		if !nombreServicio.MatchString(name) || c[name] {
			return nil, fmt.Errorf("services: invalid or duplicate catalog name")
		}
		c[name] = true
	}
	return c, nil
}

// datosServicios keeps the registry handle and its catalog snapshot together.
// Registry.Open enforces a real private directory and verifies persisted data;
// the registry revalidates state under its cross-process lock on every call.
func (d *demonio) datosServicios(req Request) (any, error) {
	if req.Comando == "servicios-solicitar" {
		if err := d.mgr.guardia.ExigirLibre(); err != nil {
			return nil, err
		}
	}
	d.servicios.mu.Lock()
	defer d.servicios.mu.Unlock()
	dir := filepath.Join(d.dir, "services")
	if _, err := asegurarDirectorio(dir); err != nil {
		return nil, err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("services: registry directory must not be a symlink")
	}
	if err := os.Chmod(dir, 0700); err != nil {
		return nil, err
	}
	catalog, err := leerCatalogoServicios(filepath.Join(dir, "catalog.json"))
	if err != nil {
		return nil, err
	}
	d.servicios.registry, err = services.Open(dir, catalog)
	if err != nil {
		return nil, err
	}
	switch req.Comando {
	case "servicios-donde":
		return d.servicios.registry.WhereCanOpen(req.Servicio)
	case "servicios-estado":
		return d.servicios.registry.Status(req.Capsula)
	case "servicios-pendientes":
		return d.servicios.registry.ListPending()
	case "servicios-solicitar":
		// Recheck after opening/locking; a stop arriving while waiting must win.
		if err := d.mgr.guardia.ExigirLibre(); err != nil {
			return nil, err
		}
		return d.servicios.registry.RequestAccess(req.AIID, req.Capsula, req.Servicio, req.Motivo)
	default:
		return nil, services.ErrInvalid
	}
}

func rcServicios(err error) int {
	switch {
	case errors.Is(err, ErrParadaActiva):
		return RCParada
	case errors.Is(err, services.ErrInvalid):
		return RCUso
	case errors.Is(err, services.ErrUnknownService):
		return 4
	case errors.Is(err, services.ErrPendingLimit), errors.Is(err, services.ErrNotPending):
		return 6
	default:
		return 5
	}
}

// servicios mirrors the exact socket allow-list. Flags precede positional
// arguments: donde SERVICE; estado CAPSULE; pendientes; solicitar AI CAPSULE
// SERVICE REASON. JSON data is printed for both human and structured clients.
func (c clienteSrf) servicios(verb string, args []string) int {
	fl := flag.NewFlagSet(verb, flag.ContinueOnError)
	socket := fl.String("socket", "", "path to srfd UDS socket")
	jsonFlag := fl.Bool("json", false, "structured JSON output")
	if rc, ok := c.parsear(fl, args); !ok {
		return rc
	}
	a := fl.Args()
	req := Request{Comando: verb}
	switch verb {
	case "servicios-donde":
		if len(a) != 1 {
			return RCUso
		}
		req.Servicio = a[0]
	case "servicios-estado":
		if len(a) != 1 {
			return RCUso
		}
		req.Capsula = a[0]
	case "servicios-pendientes":
		if len(a) != 0 {
			return RCUso
		}
	case "servicios-solicitar":
		if len(a) != 4 {
			return RCUso
		}
		req.AIID, req.Capsula, req.Servicio, req.Motivo = a[0], a[1], a[2], a[3]
	default:
		return RCUso
	}
	resp, err := EnviarComando(RutaSocket(*socket), req, 10*time.Second)
	if err != nil {
		fmt.Fprintf(c.stderr, "srf: services transport: %v\n", err)
		return 5
	}
	if !resp.OK {
		// Preserve supported refusal codes; never leak the legacy transport code 1
		// into this command family, even when an old daemon supplies it.
		rc := resp.RC
		if rc < 2 || rc > 7 {
			rc = 5
		}
		if *jsonFlag {
			if json.NewEncoder(c.stdout).Encode(Response{RC: rc, Error: resp.Error}) != nil {
				return 5
			}
		} else {
			fmt.Fprintf(c.stderr, "srf: %s: %s\n", verb, resp.Error)
		}
		return rc
	}
	if !json.Valid(resp.Datos) {
		fmt.Fprintln(c.stderr, "srf: services response has no JSON data")
		return 5
	}
	if _, err := fmt.Fprintln(c.stdout, string(resp.Datos)); err != nil {
		return 5
	}
	return 0
}
