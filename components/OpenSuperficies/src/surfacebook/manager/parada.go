package manager

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// ErrParadaActiva is returned when a mutating operation is attempted while the stop signal is in effect.
var ErrParadaActiva = errors.New("parada: mutation refused under an effective PARADA")

// RCParada is the canonical exit code for a request refused under PARADA.
const RCParada = 3

// Readings of ONE stop-signal file, exactly the three words scripts/ng/campana.sh (leer_parada) prints.
const (
	SenalLibre   = "LIBRE"
	SenalParada  = "PARADA"
	SenalAusente = "AUSENTE"
)

// PlazaSalaPorDefecto is the campaign room whose PARADA.txt is the Plaza half of the effective signal
// (the same default scripts/ng/campana.sh uses for $SRF_SALA).
const PlazaSalaPorDefecto = "/home/user/.openfresnel/plaza/sala-opensuperficies-nextgen"

// maxLineaSenal bounds how much of a signal file is read: only its first token matters.
const maxLineaSenal = 4096

// Guardia is the stop-signal reader the manager consults before every mutation.
// ParadaManager is the production implementation; tests substitute their own to
// prove that the guard is read again immediately before a dispatch.
type Guardia interface {
	// Estado reads the signal right now: ("LIBRE", true) or ("PARADA", false).
	Estado() (string, bool)
	// ExigirLibre returns ErrParadaActiva unless the signal reads LIBRE right now.
	ExigirLibre() error
}

// ParadaManager reads the EFFECTIVE stop signal, which is the pair (repository file, Plaza file):
// it is LIBRE only when BOTH files read LIBRE (INV-18, scripts/ng/campana.sh parada). Only the two
// PATHS are fixed at construction; both files are read again on every call, so a PARADA written while
// the daemon runs takes effect on the very next mutation.
type ParadaManager struct {
	repo  string
	plaza string
}

// RutaParadaRepo resolves the repository signal file: override, $SRF_PARADA_REPO, then <repository root>/PARADA.txt.
func RutaParadaRepo(override string) string {
	if override != "" {
		return override
	}
	if env := os.Getenv("SRF_PARADA_REPO"); env != "" {
		return env
	}
	return filepath.Join(findRepoRoot(), "PARADA.txt")
}

// RutaParadaPlaza resolves the Plaza signal file: override, $SRF_PARADA_PLAZA, then $SRF_SALA/PARADA.txt,
// then the campaign room's PARADA.txt.
func RutaParadaPlaza(override string) string {
	if override != "" {
		return override
	}
	if env := os.Getenv("SRF_PARADA_PLAZA"); env != "" {
		return env
	}
	if sala := os.Getenv("SRF_SALA"); sala != "" {
		return filepath.Join(sala, "PARADA.txt")
	}
	return filepath.Join(PlazaSalaPorDefecto, "PARADA.txt")
}

// NuevoParadaManager creates the reader of the effective signal. An empty argument selects the default
// resolution of that file (RutaParadaRepo, RutaParadaPlaza).
func NuevoParadaManager(repoOverride, plazaOverride string) *ParadaManager {
	return &ParadaManager{repo: RutaParadaRepo(repoOverride), plaza: RutaParadaPlaza(plazaOverride)}
}

// Ruta returns the repository file this reader consults.
func (p *ParadaManager) Ruta() string { return p.repo }

// RutaPlaza returns the Plaza file this reader consults.
func (p *ParadaManager) RutaPlaza() string { return p.plaza }

// LeerSenal reads one signal file fail-closed, with the rules of campana.sh leer_parada: a path that is
// not a regular file is AUSENTE; otherwise the first blank-separated field of the FIRST line must be
// exactly "LIBRE" (a blank first line, an empty file, "LIBREX", "libre" and a file that cannot be read
// are all PARADA).
func LeerSenal(ruta string) string {
	fi, err := os.Stat(ruta)
	if err != nil || !fi.Mode().IsRegular() {
		return SenalAusente
	}
	f, err := os.Open(ruta)
	if err != nil {
		return SenalParada // unreadable
	}
	defer f.Close()

	datos, err := io.ReadAll(io.LimitReader(f, maxLineaSenal))
	if err != nil {
		return SenalParada
	}
	primera, _, _ := strings.Cut(string(datos), "\n")
	campos := strings.FieldsFunc(primera, func(r rune) bool { return r == ' ' || r == '\t' }) // awk's default field separators
	if len(campos) == 0 || campos[0] != SenalLibre {
		return SenalParada
	}
	return SenalLibre
}

// Efectiva combines the two readings: LIBRE only when both are LIBRE, PARADA in every other case
// (a missing, unreadable, empty or non-LIBRE file on either side).
func Efectiva(repo, plaza string) string {
	if repo == SenalLibre && plaza == SenalLibre {
		return SenalLibre
	}
	return SenalParada
}

// Lecturas reads both files right now and returns (repository, Plaza, effective).
func (p *ParadaManager) Lecturas() (repo, plaza, efectiva string) {
	repo, plaza = LeerSenal(p.repo), LeerSenal(p.plaza)
	return repo, plaza, Efectiva(repo, plaza)
}

// Estado reads the effective stop signal: ("LIBRE", true) only when both files read LIBRE, else ("PARADA", false).
func (p *ParadaManager) Estado() (string, bool) {
	_, _, ef := p.Lecturas()
	return ef, ef == SenalLibre
}

// ExigirLibre returns ErrParadaActiva unless the effective stop signal reads LIBRE right now.
func (p *ParadaManager) ExigirLibre() error {
	if _, libre := p.Estado(); !libre {
		return ErrParadaActiva
	}
	return nil
}
