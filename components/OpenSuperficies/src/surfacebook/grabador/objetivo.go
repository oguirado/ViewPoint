package grabador

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"
)

// ErrSinObjetivo is returned when none of the candidates pass the filtering criteria.
var ErrSinObjetivo = errors.New("grabador_sin_objetivo")

// ErrObjetivoAmbiguo is returned when multiple candidates tie with identical maximum area.
var ErrObjetivoAmbiguo = errors.New("grabador_objetivo_ambiguo")

// Candidato represents an X11 window candidate returned by window enumeration or oracle.
type Candidato struct {
	XID              uint64 `json:"xid"`
	Nombre           string `json:"nombre"`
	Clase            string `json:"clase"`
	WH               []int  `json:"wh"`
	Mapeada          bool   `json:"mapeada"`
	OverrideRedirect bool   `json:"override_redirect"`
}

// Area calculates window area in pixels. A non-positive width/height yields 0.
func (c Candidato) Area() int {
	if len(c.WH) < 2 || c.WH[0] <= 0 || c.WH[1] <= 0 {
		return 0
	}
	return c.WH[0] * c.WH[1]
}

// Desc produces a diagnostic description string for logging and error reporting.
func (c Candidato) Desc() string {
	w, h := 0, 0
	if len(c.WH) >= 2 {
		w, h = c.WH[0], c.WH[1]
	}
	return fmt.Sprintf("%s [%q %dx%d mapped=%v override_redirect=%v]",
		Hex(c.XID), c.Nombre, w, h, c.Mapeada, c.OverrideRedirect)
}

// Hex formats an XID in canonical lowercase hexadecimal with 0x prefix.
func Hex(xid uint64) string {
	return fmt.Sprintf("0x%x", xid)
}

// ElegirHost applies the dual-oracle target selection rule:
// 1. Rejects unmapped windows (avoids unmapped group leaders).
// 2. Rejects override-redirect windows (tooltips, popups, menus).
// 3. Rejects windows with zero or negative area.
// 4. Selects the window with strictly maximum area.
// 5. If two distinct XIDs tie on the maximum area, fails closed with ErrObjetivoAmbiguo. Never guess.
func ElegirHost(cands []Candidato) (Candidato, error) {
	vivos := make([]Candidato, 0, len(cands))
	rechazos := make([]string, 0, len(cands))

	for _, c := range cands {
		switch {
		case c.XID == 0:
			rechazos = append(rechazos, c.Desc()+" → xid 0")
		case !c.Mapeada:
			rechazos = append(rechazos, c.Desc()+" → NOT mapped (group leader or withdrawn)")
		case c.OverrideRedirect:
			rechazos = append(rechazos, c.Desc()+" → override-redirect (not an application toplevel)")
		case c.Area() == 0:
			rechazos = append(rechazos, c.Desc()+" → zero usable area")
		default:
			vivos = append(vivos, c)
		}
	}

	if len(vivos) == 0 {
		return Candidato{}, fmt.Errorf("%w: none of the %d candidates is a recordable window [%s]",
			ErrSinObjetivo, len(cands), strings.Join(rechazos, " · "))
	}

	mejor := vivos[0]
	empate := false

	for _, c := range vivos[1:] {
		switch {
		case c.Area() > mejor.Area():
			mejor, empate = c, false
		case c.Area() == mejor.Area() && c.XID != mejor.XID:
			empate = true
		}
	}

	if empate {
		iguales := make([]string, 0, len(vivos))
		for _, c := range vivos {
			if c.Area() == mejor.Area() {
				iguales = append(iguales, c.Desc())
			}
		}
		return Candidato{}, fmt.Errorf("%w: %d mapped windows with exactly the same area (%d px) and the rule does not disambiguate [%s]",
			ErrObjetivoAmbiguo, len(iguales), mejor.Area(), strings.Join(iguales, " · "))
	}

	return mejor, nil
}

// CargarCandidatos parses JSON bytes into a slice of Candidato, supporting both a raw array
// and an object wrapper {"candidatos": [...]}.
func CargarCandidatos(data []byte) ([]Candidato, error) {
	var list []Candidato
	if err := json.Unmarshal(data, &list); err == nil {
		return list, nil
	}

	var wrapper struct {
		Candidatos []Candidato `json:"candidatos"`
	}
	if err := json.Unmarshal(data, &wrapper); err == nil {
		return wrapper.Candidatos, nil
	}

	return nil, errors.New("invalid candidates format: expected an array or a wrapper with a candidatos field")
}

// CargarCandidatosArchivo reads a candidate JSON file from disk and parses it.
func CargarCandidatosArchivo(path string) ([]Candidato, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("error reading the candidates file: %w", err)
	}
	return CargarCandidatos(data)
}

// propiedadTexto reads an 8-bit property of a window; an absent property is the empty string.
func propiedadTexto(c *xgb.Conn, w xproto.Window, atom xproto.Atom) (string, error) {
	r, err := xproto.GetProperty(c, false, w, atom, xproto.GetPropertyTypeAny, 0, 256).Reply()
	if err != nil {
		return "", err
	}
	if r.Format != 8 {
		return "", nil
	}
	return string(r.Value), nil
}

// propiedadCardinal reads the first 32-bit value of a property; an absent property is 0.
func propiedadCardinal(c *xgb.Conn, w xproto.Window, atom xproto.Atom) (uint32, error) {
	r, err := xproto.GetProperty(c, false, w, atom, xproto.GetPropertyTypeAny, 0, 1).Reply()
	if err != nil {
		return 0, err
	}
	if r.Format != 32 || len(r.Value) < 4 {
		return 0, nil
	}
	return binary.LittleEndian.Uint32(r.Value), nil
}

// claseDeWMClass returns the class half of a WM_CLASS value ("instance\x00class\x00").
func claseDeWMClass(v string) string {
	partes := strings.Split(strings.TrimRight(v, "\x00"), "\x00")
	return partes[len(partes)-1]
}

// CandidatosDeDisplay is the out-of-process oracle: it asks the X server itself for the children of
// the root window and reads their map state, override-redirect flag, geometry, WM_NAME, WM_CLASS
// and _NET_WM_PID. With pid != 0 only the windows that _NET_WM_PID assigns to that process are
// returned (a client typically owns an unmapped leader next to its real window; ElegirHost is what
// tells them apart). The display goes through the host-display guard first.
func CandidatosDeDisplay(display string, pid int) ([]Candidato, error) {
	d, err := ValidarDisplayAnidado(display)
	if err != nil {
		return nil, err
	}
	c, err := conectarX(d.Canonico())
	if err != nil {
		return nil, fmt.Errorf("error connecting to display %s: %w", d.Canonico(), err)
	}
	defer c.Close()
	setup := xproto.Setup(c)
	if setup == nil || len(setup.Roots) == 0 {
		return nil, errors.New("empty X11 setup")
	}
	root := setup.DefaultScreen(c).Root
	atomPID, err := internarAtomo(c, "_NET_WM_PID")
	if err != nil {
		return nil, fmt.Errorf("error interning _NET_WM_PID: %w", err)
	}
	tree, err := xproto.QueryTree(c, root).Reply()
	if err != nil {
		return nil, fmt.Errorf("error querying the window tree: %w", err)
	}
	var out []Candidato
	for _, w := range tree.Children {
		cand, ok, err := candidatoDeVentana(c, w, atomPID, pid)
		if err != nil {
			return nil, err
		}
		if ok {
			out = append(out, cand)
		}
	}
	return out, nil
}

// candidatoDeVentana reads one window. A window that disappeared between QueryTree and this query
// (BadWindow) is skipped; every other failure is returned.
func candidatoDeVentana(c *xgb.Conn, w xproto.Window, atomPID xproto.Atom, pid int) (Candidato, bool, error) {
	saltar := func(err error) (Candidato, bool, error) {
		var bad xproto.WindowError
		if errors.As(err, &bad) {
			return Candidato{}, false, nil
		}
		return Candidato{}, false, fmt.Errorf("window %s: %w", Hex(uint64(w)), err)
	}
	attrs, err := xproto.GetWindowAttributes(c, w).Reply()
	if err != nil {
		return saltar(err)
	}
	geom, err := xproto.GetGeometry(c, xproto.Drawable(w)).Reply()
	if err != nil {
		return saltar(err)
	}
	wpid, err := propiedadCardinal(c, w, atomPID)
	if err != nil {
		return saltar(err)
	}
	if pid != 0 && int(wpid) != pid {
		return Candidato{}, false, nil
	}
	nombre, err := propiedadTexto(c, w, xproto.AtomWmName)
	if err != nil {
		return saltar(err)
	}
	clase, err := propiedadTexto(c, w, xproto.AtomWmClass)
	if err != nil {
		return saltar(err)
	}
	return Candidato{
		XID: uint64(w), Nombre: nombre, Clase: claseDeWMClass(clase),
		WH:               []int{int(geom.Width), int(geom.Height)},
		Mapeada:          attrs.MapState == xproto.MapStateViewable,
		OverrideRedirect: attrs.OverrideRedirect,
	}, true, nil
}
