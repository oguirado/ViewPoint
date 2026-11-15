package manager

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"opensuperficies.local/srf/src/browserid/services"
)

type catalogoServiciosPrueba struct{}

func (catalogoServiciosPrueba) HasService(s string) bool { return s == "chatgpt" }
func (catalogoServiciosPrueba) Services() []string       { return []string{"chatgpt"} }

type operadorServiciosPrueba struct{}

func (operadorServiciosPrueba) AuthorizeServices() error { return nil }

func entornoServicios(t *testing.T) (ConfigDaemon, *entorno, string) {
	t.Helper()
	e := nuevoEntorno(t, opcionesEntorno{})
	cfg := configDePrueba(t, corto(t), e)
	dir := filepath.Join(filepath.Dir(cfg.SocketPath), "services")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	escribir(t, filepath.Join(dir, "catalog.json"), `["chatgpt"]`)
	r, token, err := services.OpenOperator(dir, catalogoServiciosPrueba{}, operadorServiciosPrueba{})
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Assign(token, "capsule-1", "chatgpt"); err != nil {
		t.Fatal(err)
	}
	arrancar(t, cfg)
	return cfg, e, dir
}
func rpcServicios(t *testing.T, cfg ConfigDaemon, verb string, fields map[string]any) *Response {
	t.Helper()
	if fields == nil {
		fields = map[string]any{}
	}
	fields["comando"] = verb
	raw, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := intercambioCrudo(t, cfg.SocketPath, append(raw, '\n'), 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}
func datosServicios[T any](t *testing.T, resp *Response) T {
	t.Helper()
	if !resp.OK || resp.RC != 0 {
		t.Fatalf("response = %+v", resp)
	}
	var v T
	if err := json.Unmarshal(resp.Datos, &v); err != nil {
		t.Fatal(err)
	}
	return v
}
func TestServiciosRPC(t *testing.T) {
	cfg, _, dir := entornoServicios(t)
	q := datosServicios[services.Query](t, rpcServicios(t, cfg, "servicios-donde", map[string]any{"servicio": "chatgpt"}))
	if !q.ApprovedInCatalog || !reflect.DeepEqual(q.AvailableIn, []string{"capsule-1"}) {
		t.Fatalf("where = %+v", q)
	}
	s := datosServicios[services.CapsuleStatus](t, rpcServicios(t, cfg, "servicios-estado", map[string]any{"capsula": "capsule-1"}))
	if !reflect.DeepEqual(s.Services, []string{"chatgpt"}) {
		t.Fatalf("status = %+v", s)
	}
	p := datosServicios[[]services.Request](t, rpcServicios(t, cfg, "servicios-pendientes", nil))
	if len(p) != 0 {
		t.Fatal(p)
	}
	req := datosServicios[services.Request](t, rpcServicios(t, cfg, "servicios-solicitar", map[string]any{"ai_id": "ai-a", "capsula": "capsule-2", "servicio": "chatgpt", "motivo": "research"}))
	if req.State != "pending" || req.AIID != "ai-a" || req.Reason != "research" {
		t.Fatalf("request = %+v", req)
	}
	p = datosServicios[[]services.Request](t, rpcServicios(t, cfg, "servicios-pendientes", nil))
	if len(p) != 1 || p[0].ID != req.ID {
		t.Fatalf("pending = %+v", p)
	}
	s = datosServicios[services.CapsuleStatus](t, rpcServicios(t, cfg, "servicios-estado", map[string]any{"capsula": "capsule-2"}))
	if len(s.Services) != 0 {
		t.Fatalf("request granted access: %+v", s)
	}
	// The cockpit owns a separate operator handle to the same persisted registry.
	op, token, err := services.OpenOperator(dir, catalogoServiciosPrueba{}, operadorServiciosPrueba{})
	if err != nil {
		t.Fatal(err)
	}
	if err := op.Approve(token, req.ID); err != nil {
		t.Fatal(err)
	}
	s = datosServicios[services.CapsuleStatus](t, rpcServicios(t, cfg, "servicios-estado", map[string]any{"capsula": "capsule-2"}))
	if !reflect.DeepEqual(s.Services, []string{"chatgpt"}) {
		t.Fatalf("operator decision not visible: %+v", s)
	}
	p = datosServicios[[]services.Request](t, rpcServicios(t, cfg, "servicios-pendientes", nil))
	if len(p) != 0 {
		t.Fatalf("decided request still pending: %+v", p)
	}
	info, err := os.Stat(dir)
	if err != nil || info.Mode().Perm() != 0700 {
		t.Fatalf("private registry: %v %v", info, err)
	}
}
func TestServiciosRechazaVerbosDeEscritura(t *testing.T) {
	cfg, _, dir := entornoServicios(t)
	before, err := os.ReadFile(filepath.Join(dir, "assignments.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, verb := range []string{"servicios-desconocido", "servicios-asignar", "servicios-desasignar", "servicios-aprobar", "servicios-denegar", "servicios-approve", "servicios-deny"} {
		resp := rpcServicios(t, cfg, verb, map[string]any{"capsula": "capsule-2", "servicio": "chatgpt"})
		if resp.OK || resp.RC != RCUso || len(resp.Datos) != 0 {
			t.Fatalf("%s = %+v", verb, resp)
		}
		if rc, _, _ := ejecutarSrf(verb); rc != RCUso {
			t.Fatalf("CLI %s = %d", verb, rc)
		}
	}
	after, err := os.ReadFile(filepath.Join(dir, "assignments.json"))
	if err != nil || !bytes.Equal(before, after) {
		t.Fatal("refused verb modified registry", err)
	}
}
func TestServiciosCorrupcionFallaCerrado(t *testing.T) {
	cfg, _, dir := entornoServicios(t)
	datosServicios[[]services.Request](t, rpcServicios(t, cfg, "servicios-pendientes", nil))
	escribir(t, filepath.Join(dir, "assignments.json"), "corrupt\n")
	for _, verb := range []string{"servicios-donde", "servicios-estado", "servicios-pendientes", "servicios-solicitar"} {
		resp := rpcServicios(t, cfg, verb, map[string]any{"servicio": "chatgpt", "capsula": "capsule-1", "ai_id": "ai-a"})
		if resp.OK || resp.RC != 5 || len(resp.Datos) != 0 {
			t.Fatalf("%s = %+v", verb, resp)
		}
	}
}
func TestServiciosParada(t *testing.T) {
	cfg, e, dir := entornoServicios(t)
	escribir(t, e.parada, paradaActiva)
	for _, verb := range []string{"servicios-donde", "servicios-estado", "servicios-pendientes"} {
		resp := rpcServicios(t, cfg, verb, map[string]any{"servicio": "chatgpt", "capsula": "capsule-1"})
		if !resp.OK {
			t.Fatalf("read %s under PARADA = %+v", verb, resp)
		}
	}
	for _, fields := range []map[string]any{nil, {"ai_id": 42}, {"ai_id": "ai-a", "capsula": "capsule-2", "servicio": "chatgpt"}} {
		resp := rpcServicios(t, cfg, "servicios-solicitar", fields)
		if resp.OK || resp.RC != RCParada {
			t.Fatalf("mutation under PARADA = %+v", resp)
		}
	}
	r, err := services.Open(dir, catalogoServiciosPrueba{})
	if err != nil {
		t.Fatal(err)
	}
	pending, err := r.ListPending()
	if err != nil || len(pending) != 0 {
		t.Fatalf("stopped request persisted: %v %v", pending, err)
	}
}
func TestServiciosCLI(t *testing.T) {
	cfg, _, _ := entornoServicios(t)
	for _, tc := range []struct {
		verb string
		args []string
		want string
	}{
		{"servicios-donde", []string{"chatgpt"}, `"available_in":["capsule-1"]`},
		{"servicios-estado", []string{"capsule-1"}, `"services":["chatgpt"]`},
		{"servicios-solicitar", []string{"ai-cli", "capsule-2", "chatgpt", "CLI research"}, `"state":"pending"`},
		{"servicios-pendientes", nil, `"ai_id":"ai-cli"`},
	} {
		args := append([]string{tc.verb, "--socket", cfg.SocketPath, "--json"}, tc.args...)
		rc, out, stderr := ejecutarSrf(args...)
		if rc != 0 || !bytes.Contains([]byte(out), []byte(tc.want)) {
			t.Fatalf("%v = %d %s %s", args, rc, out, stderr)
		}
		if rc, _, _ := ejecutarSrf(tc.verb, "extra", "arguments", "invalid"); rc != RCUso {
			t.Fatalf("arity rc=%d", rc)
		}
	}
	if rc, _, _ := ejecutarSrf("servicios-pendientes", "--socket", filepath.Join(corto(t), "absent.sock")); rc != 5 {
		t.Fatalf("transport rc=%d", rc)
	}
}

func TestServiciosCatalogoFallaCerrado(t *testing.T) {
	for _, content := range []string{"", "null", `["chatgpt","chatgpt"]`, `["Invalid"]`, `["chatgpt"] {}`, `{}`} {
		t.Run(content, func(t *testing.T) {
			cfg, _, dir := entornoServicios(t)
			escribir(t, filepath.Join(dir, "catalog.json"), content)
			resp := rpcServicios(t, cfg, "servicios-pendientes", nil)
			if resp.OK || resp.RC != 5 {
				t.Fatalf("invalid catalog = %+v", resp)
			}
		})
	}
	t.Run("removed file", func(t *testing.T) {
		cfg, _, dir := entornoServicios(t)
		if err := os.Remove(filepath.Join(dir, "catalog.json")); err != nil {
			t.Fatal(err)
		}
		resp := rpcServicios(t, cfg, "servicios-pendientes", nil)
		if resp.OK || resp.RC != 5 {
			t.Fatalf("missing catalog = %+v", resp)
		}
	})
	t.Run("symlink", func(t *testing.T) {
		cfg, _, dir := entornoServicios(t)
		if err := os.Rename(filepath.Join(dir, "catalog.json"), filepath.Join(dir, "names.json")); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink("names.json", filepath.Join(dir, "catalog.json")); err != nil {
			t.Fatal(err)
		}
		resp := rpcServicios(t, cfg, "servicios-pendientes", nil)
		if resp.OK || resp.RC != 5 {
			t.Fatalf("symlink catalog = %+v", resp)
		}
	})
}
func TestServiciosRecargaCatalogoYValida(t *testing.T) {
	cfg, _, dir := entornoServicios(t)
	datosServicios[services.Query](t, rpcServicios(t, cfg, "servicios-donde", map[string]any{"servicio": "chatgpt"}))
	escribir(t, filepath.Join(dir, "catalog.json"), `[]`)
	q := datosServicios[services.Query](t, rpcServicios(t, cfg, "servicios-donde", map[string]any{"servicio": "chatgpt"}))
	if q.ApprovedInCatalog || len(q.AvailableIn) != 0 {
		t.Fatalf("stale catalog = %+v", q)
	}
	resp := rpcServicios(t, cfg, "servicios-solicitar", map[string]any{"ai_id": "ai-a", "capsula": "capsule-1", "servicio": "chatgpt"})
	if resp.OK || resp.RC != 4 {
		t.Fatalf("unknown service = %+v", resp)
	}
	resp = rpcServicios(t, cfg, "servicios-estado", map[string]any{"capsula": "invalid"})
	if resp.OK || resp.RC != RCUso {
		t.Fatalf("invalid capsule = %+v", resp)
	}
}
