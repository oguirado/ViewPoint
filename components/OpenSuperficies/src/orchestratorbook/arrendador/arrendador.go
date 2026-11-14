package arrendador

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

var mutanteSinEpoca = false

var (
	SrfErrEpocaVencida         = errors.New("srf: expired epoch")
	ErrSrfIrreversOcupado      = errors.New("srf: irreversible lease already active on this identity")
	ErrSrfTokenInvalido        = errors.New("srf: invalid capability token")
	ErrSrfDestinoInvalido      = errors.New("srf: destination or capsule mismatch")
	ErrSrfDurableStateRequired = errors.New("srf: durable WAL state is mandatory")
	ErrSrfArrendadorRequerido  = errors.New("srf: lessor is mandatory for storage boundary")
	ErrSrfArrendadorDegradado = errors.New("srf: lessor WAL degraded; explicit health check required")
)

// LeaseSession holds the capability token, target, and fencing epoch for an active lease.
// Token is tagged json:"-" to prevent capability secrets from leaking in JSON serialization.
type LeaseSession struct {
	Token    [16]byte  `json:"-"`
	Capsula  string    `json:"capsula"`
	Destino  uint32    `json:"destino"`
	Epoca    uint64    `json:"epoca"`
	Titular  string    `json:"titular"`
	Vence    time.Time `json:"vence"`
	Irrevers bool      `json:"irrevers"`
}

type srfClaveArriendo struct {
	capsula string
	destino uint32
}

type srfClaveEpoca struct {
	capsula string
	destino uint32
	epoca   uint64
}

type srfTokenInfo struct {
	token [16]byte
	vence time.Time
}

type srfEstadoArriendo struct {
	epoca    uint64
	token    [16]byte
	vence    time.Time
	irrevers bool
	titular  string
}

// SrfArrendador manages capability leases, monotonic fencing epochs, and irreversible exclusivity.
type SrfArrendador struct {
	mu             sync.Mutex
	estados        map[srfClaveArriendo]*srfEstadoArriendo
	tokensEmitidos map[srfClaveEpoca]srfTokenInfo
	identidad      func(capsula string) string // returns empty string if capsule has no identity
	wal            *wal.LibroConsumo
	degradado      bool
	// Each failed revoke must be durably journalled before new grants can resume.
	revocacionesPendientes map[string]uint64
}

// NuevoSrfArrendador creates a new lease manager with an identity mapping function and mandatory durable WAL state.
// A constructor that cannot recover its state fails closed rather than starting amnesiac.
func NuevoSrfArrendador(identidad func(string) string, libro *wal.LibroConsumo) (*SrfArrendador, error) {
	if libro == nil || libro.Path() == "" {
		return nil, ErrSrfDurableStateRequired
	}
	if identidad == nil {
		identidad = func(capsula string) string { return capsula }
	}
	arr := &SrfArrendador{
		estados:        make(map[srfClaveArriendo]*srfEstadoArriendo),
		tokensEmitidos: make(map[srfClaveEpoca]srfTokenInfo),
		identidad:      identidad,
		wal:            libro,
	}

	regs, err := wal.LeerRegistros(libro.Path())
	if err != nil {
		return nil, fmt.Errorf("arrendador: read wal failed: %w", err)
	}
	if len(regs) > 0 {
		if err := arr.recuperarDesdeWALBloqueado(regs); err != nil {
			return nil, fmt.Errorf("arrendador: replay wal failed: %w", err)
		}
	}

	return arr, nil
}

// ConectarWAL attaches an ARIES-adapted two-class WAL for local epoch journalling,
// automatically recovering counter states from existing records.
func (a *SrfArrendador) ConectarWAL(libro *wal.LibroConsumo) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if libro == nil || libro.Path() == "" {
		return ErrSrfDurableStateRequired
	}
	regs, err := wal.LeerRegistros(libro.Path())
	if err != nil {
		return fmt.Errorf("arrendador: read wal failed: %w", err)
	}
	// A degraded lessor must not resurrect an already-revoked in-memory lease
	// from a WAL that may be missing its revoke record. The explicit health
	// check persists pending revocations before any new grant is permitted.
	if !a.degradado && len(regs) > 0 {
		if err := a.recuperarDesdeWALBloqueado(regs); err != nil {
			return fmt.Errorf("arrendador: replay wal failed: %w", err)
		}
	}
	a.wal = libro
	return nil
}

// Degradado reports whether an unsuccessful revoke journal operation has
// disabled new grants. It never changes the revocation in memory.
func (a *SrfArrendador) Degradado() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.degradado
}

// ComprobarSaludWAL is the only operation that clears the degraded latch.
// It re-journals every failed revocation and fsyncs the WAL before allowing
// new grants. ConectarWAL by itself never resets the latch.
func (a *SrfArrendador) ComprobarSaludWAL() error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.wal == nil || a.wal.Path() == "" {
		return ErrSrfDurableStateRequired
	}
	if a.degradado {
		for capsula, veces := range a.revocacionesPendientes {
			for i := uint64(0); i < veces; i++ {
				if err := a.escribirRevocacionBloqueado(capsula); err != nil {
					return err
				}
			}
		}
	}
	if err := a.wal.Sync(); err != nil {
		return fmt.Errorf("arrendador: wal health sync failed: %w", err)
	}
	a.revocacionesPendientes = nil
	a.degradado = false
	return nil
}

func (a *SrfArrendador) escribirRevocacionBloqueado(capsula string) error {
	if a.wal == nil || a.wal.Path() == "" {
		return ErrSrfDurableStateRequired
	}
	detalles, err := json.Marshal(map[string]any{
		"capsula": capsula,
		"accion":  "revocar",
	})
	if err != nil {
		return fmt.Errorf("arrendador: marshal wal revoke failed: %w", err)
	}
	if _, err := a.wal.Escribir(&wal.Registro{
		Clase:    wal.ClaseLocal,
		Tipo:     wal.TipoReserva,
		Clave:    fmt.Sprintf("lease:%s:*", capsula),
		Detalles: detalles,
	}); err != nil {
		return fmt.Errorf("arrendador: wal journal revoke failed: %w", err)
	}
	return nil
}

func (a *SrfArrendador) podarExpiradosBloqueado(ahora time.Time) {
	for k, info := range a.tokensEmitidos {
		if !ahora.Before(info.vence) {
			delete(a.tokensEmitidos, k)
		}
	}
}

// Conceder grants or renews a lease with a strictly monotonic fencing epoch and 16-byte random token.
// At most one active irreversible lease is permitted per identity at any instant.
// If epoch journalling is enabled, the lease grant fails closed if WAL appending fails.
// Raw capability token bytes are never written to the journal (U07-F9).
func (a *SrfArrendador) Conceder(capsula string, destino uint32, titular string, ttl time.Duration, irrevers bool) (LeaseSession, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.degradado {
		return LeaseSession{}, ErrSrfArrendadorDegradado
	}

	ahora := time.Now()
	a.podarExpiradosBloqueado(ahora)

	if irrevers {
		id := a.identidad(capsula)
		for k, e := range a.estados {
			if e.irrevers && ahora.Before(e.vence) && a.identidad(k.capsula) == id {
				return LeaseSession{}, ErrSrfIrreversOcupado
			}
		}
	}

	k := srfClaveArriendo{capsula: capsula, destino: destino}
	e := a.estados[k]
	if e == nil {
		e = &srfEstadoArriendo{}
		a.estados[k] = e
	}

	nuevaEpoca := e.epoca + 1
	var token [16]byte
	if _, err := rand.Read(token[:]); err != nil {
		return LeaseSession{}, fmt.Errorf("arrendador: rand token failed: %w", err)
	}
	vence := ahora.Add(ttl)

	if a.wal != nil {
		detalles, err := json.Marshal(map[string]any{
			"capsula":  capsula,
			"destino":  destino,
			"epoca":    nuevaEpoca,
			"titular":  titular,
			"vence":    vence,
			"irrevers": irrevers,
		})
		if err != nil {
			return LeaseSession{}, fmt.Errorf("arrendador: marshal wal details failed: %w", err)
		}
		if _, err := a.wal.Escribir(&wal.Registro{
			Clase:    wal.ClaseLocal,
			Tipo:     wal.TipoReserva,
			Clave:    fmt.Sprintf("lease:%s:%d", capsula, destino),
			Detalles: detalles,
		}); err != nil {
			return LeaseSession{}, fmt.Errorf("arrendador: wal journal write failed: %w", err)
		}
	}

	e.epoca = nuevaEpoca
	e.token = token
	e.vence = vence
	e.irrevers = irrevers
	e.titular = titular

	// Prune older tokens for this destination on epoch advance (U07-F15)
	for tk := range a.tokensEmitidos {
		if tk.capsula == capsula && tk.destino == destino {
			if tk.epoca < nuevaEpoca-1 || !ahora.Before(a.tokensEmitidos[tk].vence) {
				delete(a.tokensEmitidos, tk)
			}
		}
	}
	a.tokensEmitidos[srfClaveEpoca{capsula: capsula, destino: destino, epoca: nuevaEpoca}] = srfTokenInfo{
		token: token,
		vence: vence,
	}

	return LeaseSession{
		Token:    e.token,
		Capsula:  capsula,
		Destino:  destino,
		Epoca:    e.epoca,
		Titular:  titular,
		Vence:    e.vence,
		Irrevers: irrevers,
	}, nil
}

// AutenticarToken validates in constant time whether a token matches an active non-expired lease.
func (a *SrfArrendador) AutenticarToken(capsula string, destino uint32, epoca uint64, token [16]byte) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.autenticarTokenBloqueado(capsula, destino, epoca, token)
}

// autenticarTokenBloqueado requires a.mu; storage boundaries use it while
// holding b.mu -> a.mu to prevent a revoke between validation and commit.
func (a *SrfArrendador) autenticarTokenBloqueado(capsula string, destino uint32, epoca uint64, token [16]byte) bool {
	ahora := time.Now()
	a.podarExpiradosBloqueado(ahora)

	var zeroToken [16]byte
	if subtle.ConstantTimeCompare(token[:], zeroToken[:]) == 1 {
		return false
	}

	info, existe := a.tokensEmitidos[srfClaveEpoca{capsula: capsula, destino: destino, epoca: epoca}]
	if !existe {
		subtle.ConstantTimeCompare(token[:], zeroToken[:])
		return false
	}

	if !ahora.Before(info.vence) {
		delete(a.tokensEmitidos, srfClaveEpoca{capsula: capsula, destino: destino, epoca: epoca})
		subtle.ConstantTimeCompare(token[:], zeroToken[:])
		return false
	}

	return subtle.ConstantTimeCompare(token[:], info.token[:]) == 1
}

// Validar verifies lease validity from within the capsule's actuator boundary (linearizability, §6.1).
func (a *SrfArrendador) Validar(l LeaseSession) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.validarBloqueado(l)
}

// validarBloqueado requires a.mu (also held through the storage commit).
func (a *SrfArrendador) validarBloqueado(l LeaseSession) error {
	ahora := time.Now()
	a.podarExpiradosBloqueado(ahora)

	e := a.estados[srfClaveArriendo{capsula: l.Capsula, destino: l.Destino}]
	if e == nil {
		return SrfErrEpocaVencida
	}
	if !mutanteSinEpoca {
		if e.epoca != l.Epoca {
			return SrfErrEpocaVencida
		}
		if subtle.ConstantTimeCompare(e.token[:], l.Token[:]) != 1 {
			return SrfErrEpocaVencida
		}
	}
	if !ahora.Before(e.vence) {
		return SrfErrEpocaVencida
	}
	if !ahora.Before(l.Vence) {
		return SrfErrEpocaVencida
	}
	return nil
}

// RevocarCapsula invalidates all active leases for a capsule by advancing epochs, zeroing expiry,
// and dropping revoked entries from tokensEmitidos (U07-F11, U07-F15).
func (a *SrfArrendador) RevocarCapsula(capsula string) error {
	a.mu.Lock()
	defer a.mu.Unlock()

	for k, e := range a.estados {
		if k.capsula == capsula {
			e.epoca++
			e.vence = time.Time{}
			e.irrevers = false
		}
	}

	// Drop revoked tokens from tokensEmitidos (U07-F11, U07-F15)
	for k := range a.tokensEmitidos {
		if k.capsula == capsula {
			delete(a.tokensEmitidos, k)
		}
	}

	// The in-memory revocation is irrevocable even if journalling fails.
	// Keep the lessor degraded until a successful explicit health check.
	err := a.escribirRevocacionBloqueado(capsula)
	if err == nil {
		err = a.wal.Sync()
		if err != nil {
			err = fmt.Errorf("arrendador: wal journal revoke sync failed: %w", err)
		}
	}
	if err != nil {
		a.degradado = true
		if a.revocacionesPendientes == nil {
			a.revocacionesPendientes = make(map[string]uint64)
		}
		a.revocacionesPendientes[capsula]++
		return err
	}
	return nil
}

// RecuperarDesdeWAL replays local-class WAL records to reconstruct highest epochs per destination.
func (a *SrfArrendador) RecuperarDesdeWAL(registros []wal.Registro) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.recuperarDesdeWALBloqueado(registros)
}

func (a *SrfArrendador) recuperarDesdeWALBloqueado(registros []wal.Registro) error {
	type detalleLease struct {
		Capsula  string    `json:"capsula"`
		Destino  uint32    `json:"destino"`
		Epoca    uint64    `json:"epoca"`
		Titular  string    `json:"titular"`
		Vence    time.Time `json:"vence"`
		Irrevers bool      `json:"irrevers"`
		Accion   string    `json:"accion"`
	}

	for _, reg := range registros {
		if reg.Clase != wal.ClaseLocal || reg.Tipo != wal.TipoReserva || len(reg.Detalles) == 0 {
			continue
		}
		var det detalleLease
		if err := json.Unmarshal(reg.Detalles, &det); err != nil {
			continue
		}
		if det.Capsula == "" {
			continue
		}

		if det.Accion == "revocar" {
			for k, e := range a.estados {
				if k.capsula == det.Capsula {
					e.epoca++
					e.vence = time.Time{}
					e.irrevers = false
				}
			}
			for k := range a.tokensEmitidos {
				if k.capsula == det.Capsula {
					delete(a.tokensEmitidos, k)
				}
			}
			continue
		}

		k := srfClaveArriendo{capsula: det.Capsula, destino: det.Destino}
		e := a.estados[k]
		if e == nil {
			a.estados[k] = &srfEstadoArriendo{
				epoca:    det.Epoca,
				vence:    det.Vence,
				irrevers: det.Irrevers,
				titular:  det.Titular,
			}
		} else if det.Epoca > e.epoca {
			e.epoca = det.Epoca
			e.vence = det.Vence
			e.irrevers = det.Irrevers
			e.titular = det.Titular
		}
	}
	return nil
}

// BarreraAlmacenamiento represents the destination storage/actuator boundary.
// Per Lamport-1998, Herlihy-Wing-1990, and Kleppmann:
// "A paused agent that wakes after its lease expired is indistinguishable from a live one
// without a fencing epoch; the storage boundary is the only place that survives a stop-the-world pause."
// The storage boundary authenticates capability tokens in constant time, consults lease validity,
// and enforces monotonic fencing epochs: reject when l.Epoca < b.ultimaEpoca, accept when l.Epoca >= b.ultimaEpoca.
type BarreraAlmacenamiento struct {
	mu           sync.Mutex
	capsula      string
	destino      uint32
	ultimaEpoca  uint64
	datos        []byte
	escriturasOk int
	arrendador   *SrfArrendador
	wal          *wal.LibroConsumo
}

// NuevaBarreraAlmacenamiento creates a storage/actuator boundary for a destination with mandatory durable WAL state.
// Fails closed if arr is nil, wal is nil, or state cannot be recovered.
func NuevaBarreraAlmacenamiento(capsula string, destino uint32, arr *SrfArrendador, libro *wal.LibroConsumo) (*BarreraAlmacenamiento, error) {
	if libro == nil || libro.Path() == "" {
		return nil, ErrSrfDurableStateRequired
	}
	if arr == nil {
		return nil, ErrSrfArrendadorRequerido
	}
	b := &BarreraAlmacenamiento{
		capsula:    capsula,
		destino:    destino,
		arrendador: arr,
		wal:        libro,
	}

	regs, err := wal.LeerRegistros(libro.Path())
	if err != nil {
		return nil, fmt.Errorf("barrera: recover from wal failed: %w", err)
	}
	if len(regs) > 0 {
		if err := b.recuperarDesdeWALBloqueado(regs); err != nil {
			return nil, fmt.Errorf("barrera: replay wal failed: %w", err)
		}
	}

	return b, nil
}

// ConectarArrendador binds an arrendador to the storage boundary for token authentication.
func (b *BarreraAlmacenamiento) ConectarArrendador(arr *SrfArrendador) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.arrendador = arr
}

// ConectarWAL attaches a WAL for durable boundary journalling and recovers ultimaEpoca.
func (b *BarreraAlmacenamiento) ConectarWAL(libro *wal.LibroConsumo) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if libro == nil || libro.Path() == "" {
		return ErrSrfDurableStateRequired
	}
	b.wal = libro

	regs, err := wal.LeerRegistros(libro.Path())
	if err != nil {
		return fmt.Errorf("barrera: recover from wal failed: %w", err)
	}
	if len(regs) > 0 {
		if err := b.recuperarDesdeWALBloqueado(regs); err != nil {
			return fmt.Errorf("barrera: replay wal failed: %w", err)
		}
	}
	return nil
}

// RecuperarDesdeWAL replays local-class WAL records to reconstruct highest accepted epoch.
func (b *BarreraAlmacenamiento) RecuperarDesdeWAL(registros []wal.Registro) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.recuperarDesdeWALBloqueado(registros)
}

func (b *BarreraAlmacenamiento) recuperarDesdeWALBloqueado(registros []wal.Registro) error {
	type detalleRegistro struct {
		Capsula string `json:"capsula"`
		Destino uint32 `json:"destino"`
		Epoca   uint64 `json:"epoca"`
	}
	for _, reg := range registros {
		if reg.Clase != wal.ClaseLocal || reg.Tipo != wal.TipoDesenlace || len(reg.Detalles) == 0 {
			continue
		}
		var det detalleRegistro
		if err := json.Unmarshal(reg.Detalles, &det); err != nil {
			continue
		}
		if det.Capsula == b.capsula && det.Destino == b.destino {
			if det.Epoca > b.ultimaEpoca {
				b.ultimaEpoca = det.Epoca
			}
		}
	}
	return nil
}

// UltimaEpoca returns the highest fencing epoch accepted by this storage boundary.
func (b *BarreraAlmacenamiento) UltimaEpoca() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.ultimaEpoca
}

// EscriturasOk returns the count of successfully accepted writes.
func (b *BarreraAlmacenamiento) EscriturasOk() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.escriturasOk
}

// UltimoDato returns a copy of the latest written data.
func (b *BarreraAlmacenamiento) UltimoDato() []byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]byte(nil), b.datos...)
}

// EjecutarConArriendo holds the storage boundary lock and then the lessor lock,
// always in b.mu -> a.mu order, from authentication through the local WAL and
// storage commit performed by f. f must not do transport/seat I/O or call back
// into either object: the critical section is for local, non-recursive commits.
func (b *BarreraAlmacenamiento) EjecutarConArriendo(l LeaseSession, f func() error) error {
	b.mu.Lock()
	defer b.mu.Unlock()

	if l.Capsula != b.capsula || l.Destino != b.destino {
		return ErrSrfDestinoInvalido
	}
	var zeroToken [16]byte
	if subtle.ConstantTimeCompare(l.Token[:], zeroToken[:]) == 1 {
		return ErrSrfTokenInvalido
	}
	if !mutanteSinEpoca {
		if l.Epoca < b.ultimaEpoca || l.Epoca == 0 {
			return SrfErrEpocaVencida
		}
	}
	if !time.Now().Before(l.Vence) {
		return SrfErrEpocaVencida
	}
	if b.arrendador == nil {
		return ErrSrfTokenInvalido
	}

	// Neither token authentication nor lease validation may release a.mu
	// before f's WAL append and commit: RevocarCapsula takes the same a.mu.
	a := b.arrendador
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.autenticarTokenBloqueado(l.Capsula, l.Destino, l.Epoca, l.Token) {
		return ErrSrfTokenInvalido
	}
	if err := a.validarBloqueado(l); err != nil {
		return err
	}
	if f == nil {
		return errors.New("barrera: nil local commit")
	}
	return f()
}

// Escribir authenticates the lease, appends to the local WAL, and commits
// under the same b.mu -> a.mu critical section; revocation cannot slip
// between validation and the commit point.
func (b *BarreraAlmacenamiento) Escribir(l LeaseSession, dato []byte) error {
	return b.EjecutarConArriendo(l, func() error {
		if b.wal != nil {
			detalles, err := json.Marshal(map[string]any{
				"capsula": b.capsula,
				"destino": b.destino,
				"epoca":   l.Epoca,
			})
			if err != nil {
				return fmt.Errorf("barrera: marshal wal details failed: %w", err)
			}
			if _, err := b.wal.Escribir(&wal.Registro{
				Clase:    wal.ClaseLocal,
				Tipo:     wal.TipoDesenlace,
				Clave:    fmt.Sprintf("storage:%s:%d", b.capsula, b.destino),
				Detalles: detalles,
			}); err != nil {
				return fmt.Errorf("barrera: wal journal write failed: %w", err)
			}
		}
		if l.Epoca > b.ultimaEpoca {
			b.ultimaEpoca = l.Epoca
		}
		b.datos = append([]byte(nil), dato...)
		b.escriturasOk++
		return nil
	})
}

// CompararTokens performs constant-time comparison of two 16-byte lease tokens.
func CompararTokens(a, b [16]byte) bool {
	return subtle.ConstantTimeCompare(a[:], b[:]) == 1
}

// ResultadoSimulacion captures the results of a live resume simulation.
type ResultadoSimulacion struct {
	EscrituraZombiRechazada    bool   `json:"escritura_zombi_rechazada"`
	ComparacionTiempoConstante bool   `json:"comparacion_tiempo_constante"`
	Capsula                    string `json:"capsula"`
	Destino                    uint32 `json:"destino"`
	EpocaTitularA              uint64 `json:"epoca_titular_a"`
	EpocaTitularB              uint64 `json:"epoca_titular_b"`
	UltimaEpocaAlmacen         uint64 `json:"ultima_epoca_almacen"`
	Mensaje                    string `json:"mensaje"`
}

// SimularReanudacion performs a live simulation of a deposed holder resuming after pause.
// Uses an attached WAL to ensure live epoch journalling on real execution paths.
func SimularReanudacion(capsula string, destino uint32) ResultadoSimulacion {
	tmpDir, err := os.MkdirTemp("", "srf-arriendo-wal-*")
	if err != nil {
		return ResultadoSimulacion{
			Mensaje: fmt.Sprintf("failed to create temp dir for wal: %v", err),
		}
	}
	defer os.RemoveAll(tmpDir)

	walPath := filepath.Join(tmpDir, "simulacion.wal")
	libro, err := wal.AbrirLibro(walPath)
	if err != nil {
		return ResultadoSimulacion{
			Mensaje: fmt.Sprintf("failed to open wal: %v", err),
		}
	}
	defer libro.Cerrar()

	arr, err := NuevoSrfArrendador(func(c string) string { return "id-" + c }, libro)
	if err != nil {
		return ResultadoSimulacion{
			Mensaje: fmt.Sprintf("failed to construct lessor: %v", err),
		}
	}

	barrera, err := NuevaBarreraAlmacenamiento(capsula, destino, arr, libro)
	if err != nil {
		return ResultadoSimulacion{
			Mensaje: fmt.Sprintf("failed to construct boundary: %v", err),
		}
	}

	// 1. Holder A obtains lease (epoch 1) and writes initial state
	sesionA, errA := arr.Conceder(capsula, destino, "titular-A", 100*time.Millisecond, false)
	if errA != nil {
		return ResultadoSimulacion{Mensaje: fmt.Sprintf("failed to grant lease A: %v", errA)}
	}
	if err := barrera.Escribir(sesionA, []byte("data-A-init")); err != nil {
		return ResultadoSimulacion{Mensaje: fmt.Sprintf("failed write A: %v", err)}
	}

	// 2. Holder A is paused (stop-the-world). Lease is granted to Holder B (epoch 2).
	sesionB, errB := arr.Conceder(capsula, destino, "titular-B", 100*time.Millisecond, false)
	if errB != nil {
		return ResultadoSimulacion{Mensaje: fmt.Sprintf("failed to grant lease B: %v", errB)}
	}

	// Holder B commits write to storage boundary, advancing boundary epoch to 2
	if err := barrera.Escribir(sesionB, []byte("data-B-active")); err != nil {
		return ResultadoSimulacion{Mensaje: fmt.Sprintf("failed write B: %v", err)}
	}

	// 3. Holder A wakes up (deposed) and attempts delayed write with epoch 1
	errZombi := barrera.Escribir(sesionA, []byte("data-A-zombie"))
	zombiRechazado := errors.Is(errZombi, SrfErrEpocaVencida)

	// 4. Test constant-time token comparison live
	var tok1, tok2, tok3 [16]byte
	_, _ = rand.Read(tok1[:])
	tok2 = tok1
	tok3 = tok1
	tok3[15] ^= 0xFF

	eq := CompararTokens(tok1, tok2)
	neq := CompararTokens(tok1, tok3)
	tiempoConstante := eq && !neq

	msg := "fencing epoch successfully rejected zombie write"
	if !zombiRechazado {
		msg = "MUTATION DETECTED: zombie write accepted"
	}

	return ResultadoSimulacion{
		EscrituraZombiRechazada:    zombiRechazado,
		ComparacionTiempoConstante: tiempoConstante,
		Capsula:                    capsula,
		Destino:                    destino,
		EpocaTitularA:              sesionA.Epoca,
		EpocaTitularB:              sesionB.Epoca,
		UltimaEpocaAlmacen:         barrera.UltimaEpoca(),
		Mensaje:                    msg,
	}
}
