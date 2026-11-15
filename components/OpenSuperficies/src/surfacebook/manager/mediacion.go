package manager

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/dockerapi"
)

// Census verdicts carried by SrfMediacionReport.Estado.
const (
	// CensoCompleta: both legs ran to completion and no foreign surface was observed.
	CensoCompleta = "completa"
	// CensoIncompleta: both legs ran to completion and at least one foreign surface was observed.
	CensoIncompleta = "incompleta"
	// CensoParcial: at least one leg did not run to completion, so "nothing foreign" cannot be asserted.
	CensoParcial = "censo_parcial"
)

const (
	censoTimeoutDockerDefecto = 8 * time.Second

	// Port windows of the campaign: Xpra 10020-10099 and CDP 9220-9299.
	puertoXpraMin = 10020
	puertoXpraMax = 10099
	puertoCDPMin  = 9220
	puertoCDPMax  = 9299
)

// SrfMediacionReport is the mediation census. Every field is either counted from an
// observation made by one of the two legs or derived from those counts; the two
// censo_*_ok fields state whether the corresponding leg REALLY ran to completion, so
// that ajenos==0 can be told apart from "the census did not look".
type SrfMediacionReport struct {
	Fuente             string   `json:"fuente"`             // "srfd" when produced by the daemon, "local" when produced without it
	Ajenos             int      `json:"ajenos"`             // len(contenedores_ajenos)+len(procesos_ajenos)+len(puertos_ajenos)
	Mediadas           int      `json:"mediadas"`           // capsules in the daemon's table (0 for a local census)
	Declaradas         int      `json:"declaradas"`         // declared containers + declared ports (_estado/residuos/declarados.json)
	ObservadasTotal    int      `json:"observadas_total"`   // observadas_docker + observadas_qemu + observadas_puertos
	ObservadasDocker   int      `json:"observadas_docker"`  // running campaign containers listed by the Docker Engine API
	ObservadasQEMU     int      `json:"observadas_qemu"`    // qemu-system processes found under the procfs root
	ObservadasPuertos  int      `json:"observadas_puertos"` // distinct listening TCP ports inside the campaign windows
	CensoDockerOK      bool     `json:"censo_docker_ok"`    // the Docker leg ran to completion
	CensoProcfsOK      bool     `json:"censo_procfs_ok"`    // the procfs leg ran to completion
	ContenedoresAjenos []string `json:"contenedores_ajenos"`
	ProcesosAjenos     []int    `json:"procesos_ajenos"`
	PuertosAjenos      []int    `json:"puertos_ajenos"`
	Estado             string   `json:"estado"` // CensoCompleta | CensoIncompleta | CensoParcial
	Errores            []string `json:"errores,omitempty"`
}

// RC is the exit code a census command must return: 0 only for a complete census with
// nothing foreign; 1 when a foreign surface was observed and also when a leg did not run.
func (r SrfMediacionReport) RC() int {
	if r.Estado == CensoCompleta {
		return 0
	}
	return RCFallo
}

// CensoOpciones selects where the census looks. The zero value is production: the Docker
// socket named by $DOCKER_HOST (or the default socket), /proc, this process, the repository root.
type CensoOpciones struct {
	DockerSocket  string
	ProcDir       string
	SelfPID       int
	RepoDir       string
	TimeoutDocker time.Duration
}

func (o CensoOpciones) conDefectos() CensoOpciones {
	if o.ProcDir == "" {
		o.ProcDir = "/proc"
	}
	if o.SelfPID == 0 {
		o.SelfPID = os.Getpid()
	}
	if o.RepoDir == "" {
		o.RepoDir = findRepoRoot()
	}
	if o.TimeoutDocker <= 0 {
		o.TimeoutDocker = censoTimeoutDockerDefecto
	}
	return o
}

// conjuntoMediado is what the daemon itself claims to mediate.
type conjuntoMediado struct {
	total        int
	contenedores map[string]bool // names without the leading slash
	puertos      map[int]bool
	pids         map[int]bool
}

func nuevoConjuntoMediado() conjuntoMediado {
	return conjuntoMediado{
		contenedores: map[string]bool{},
		puertos:      map[int]bool{},
		pids:         map[int]bool{},
	}
}

// ResiduosDeclarados represents the surfaces the operator declared in _estado/residuos/declarados.json.
type ResiduosDeclarados struct {
	Contenedores []string `json:"contenedores"`
	Redes        []string `json:"redes"`
	Puertos      []int    `json:"puertos"`
}

// findRepoRoot locates the repository root: $SRF_REPO, else the nearest ancestor of the
// working directory holding go.mod, else the campaign default (the same one campana.sh uses).
func findRepoRoot() string {
	if env := os.Getenv("SRF_REPO"); env != "" {
		return env
	}
	dir, err := os.Getwd()
	if err != nil {
		dir = "."
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return "/home/user/OpenSuperficies"
}

// LeerDeclaraciones loads the declared surfaces. A missing file is legitimate (nothing is
// declared); an unreadable or malformed one is an error. Either way an empty declaration can
// only ADD foreign surfaces to the census, never hide one.
func LeerDeclaraciones(repoPath string) (ResiduosDeclarados, error) {
	var decl ResiduosDeclarados
	if repoPath == "" {
		repoPath = findRepoRoot()
	}
	data, err := os.ReadFile(filepath.Join(repoPath, "_estado", "residuos", "declarados.json"))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return decl, nil
		}
		return ResiduosDeclarados{}, err
	}
	if err := json.Unmarshal(data, &decl); err != nil {
		return ResiduosDeclarados{}, fmt.Errorf("malformed declarations: %w", err)
	}
	return decl, nil
}

// CensoLocal runs the census without a daemon: nothing is mediated, so every campaign
// surface that is not declared is foreign.
func CensoLocal(ctx context.Context) SrfMediacionReport {
	return censoLocalCon(ctx, CensoOpciones{})
}

func censoLocalCon(ctx context.Context, opts CensoOpciones) SrfMediacionReport {
	rep := censar(ctx, opts, nuevoConjuntoMediado())
	rep.Fuente = "local"
	return rep
}

// EjecutarMediacion is the whole of `srf-mediacion`: it prints the mediation census and returns the exit
// code of its verdict, so the code cmd/srf-mediacion runs is the code the tests run. The code is 0 only
// when both legs (Docker, procfs) ran to completion and nothing foreign was observed; it is 1 when a
// foreign surface was observed AND when a leg did not run (estado "censo_parcial"), which is the
// fail-closed evidence of the census. censo selects where a LOCAL census looks (the zero value is
// production); a census produced by srfd uses the daemon's own options.
func EjecutarMediacion(args []string, stdout, stderr io.Writer, censo CensoOpciones) int {
	flags := flag.NewFlagSet("srf-mediacion", flag.ContinueOnError)
	flags.SetOutput(stderr)
	var (
		jsonFlag   bool
		socketFlag string
	)
	flags.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	flags.StringVar(&socketFlag, "socket", "", "path to srfd UDS socket")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return RCUso
	}

	socketPath := RutaSocket(socketFlag)
	resp, err := EnviarComando(socketPath, Request{Comando: "mediacion"}, 20*time.Second)

	var rep SrfMediacionReport
	switch {
	case err == nil && resp.OK:
		if uerr := json.Unmarshal(resp.Datos, &rep); uerr != nil {
			fmt.Fprintf(stderr, "srf-mediacion: unreadable census from srfd: %v\n", uerr)
			return RCFallo
		}
	case err == nil:
		// srfd answered and refused: that is not "daemon absent", so there is no silent fallback.
		fmt.Fprintf(stderr, "srf-mediacion: srfd refused: %s\n", resp.Error)
		if resp.RC == 0 {
			return RCFallo
		}
		return resp.RC
	case errors.Is(err, syscall.ENOENT) || errors.Is(err, syscall.ECONNREFUSED):
		// Only a daemon that is not there justifies a local census, and the report says so.
		fmt.Fprintf(stderr, "srf-mediacion: srfd unreachable (%v): local census, nothing is mediated\n", err)
		rep = censoLocalCon(context.Background(), censo)
	default:
		fmt.Fprintf(stderr, "srf-mediacion: %v\n", err)
		return RCFallo
	}

	if jsonFlag {
		enc := json.NewEncoder(stdout)
		enc.SetIndent("", "  ")
		if eerr := enc.Encode(rep); eerr != nil {
			fmt.Fprintf(stderr, "srf-mediacion: encode report: %v\n", eerr)
			return RCFallo
		}
	} else {
		fmt.Fprintf(stdout, "mediacion: %s (fuente: %s, ajenos: %d, mediadas: %d, declaradas: %d, observadas: %d, docker_ok: %t, procfs_ok: %t)\n",
			rep.Estado, rep.Fuente, rep.Ajenos, rep.Mediadas, rep.Declaradas, rep.ObservadasTotal, rep.CensoDockerOK, rep.CensoProcfsOK)
	}
	return rep.RC()
}

// CensoMediacion observes the host and compares it with what this manager mediates and
// with what the operator declared.
func (m *SurfaceManager) CensoMediacion(ctx context.Context) SrfMediacionReport {
	rep := censar(ctx, m.censo, m.conjuntoMediado())
	rep.Fuente = "srfd"
	return rep
}

// censar runs both legs and states, for each, whether it ran to completion.
func censar(ctx context.Context, opts CensoOpciones, med conjuntoMediado) SrfMediacionReport {
	opts = opts.conDefectos()

	rep := SrfMediacionReport{
		Mediadas:           med.total,
		ContenedoresAjenos: []string{},
		ProcesosAjenos:     []int{},
		PuertosAjenos:      []int{},
	}

	decl, derr := LeerDeclaraciones(opts.RepoDir)
	if derr != nil {
		rep.Errores = append(rep.Errores, "declarados: "+derr.Error())
	}
	declContenedores := map[string]bool{}
	for _, c := range decl.Contenedores {
		declContenedores[strings.TrimPrefix(c, "/")] = true
	}
	declPuertos := map[int]bool{}
	for _, p := range decl.Puertos {
		declPuertos[p] = true
	}
	rep.Declaradas = len(decl.Contenedores) + len(decl.Puertos)

	// Leg 1: Docker Engine API.
	obsDocker, ajenosDocker, err := censarDocker(ctx, opts, med, declContenedores)
	rep.ObservadasDocker = obsDocker
	rep.ContenedoresAjenos = ajenosDocker
	rep.CensoDockerOK = err == nil
	if err != nil {
		rep.Errores = append(rep.Errores, "docker: "+unaLinea(err))
	}

	// Leg 2: procfs (qemu processes and listening TCP ports).
	pr := censarProcfs(opts, med, declPuertos)
	rep.ObservadasQEMU = pr.qemuObservadas
	rep.ProcesosAjenos = pr.procesosAjenos
	rep.ObservadasPuertos = pr.puertosObservados
	rep.PuertosAjenos = pr.puertosAjenos
	err = errors.Join(pr.errs...)
	rep.CensoProcfsOK = err == nil
	if err != nil {
		rep.Errores = append(rep.Errores, "procfs: "+unaLinea(err))
	}

	rep.ObservadasTotal = rep.ObservadasDocker + rep.ObservadasQEMU + rep.ObservadasPuertos
	rep.Ajenos = len(rep.ContenedoresAjenos) + len(rep.ProcesosAjenos) + len(rep.PuertosAjenos)

	switch {
	case !rep.CensoDockerOK || !rep.CensoProcfsOK:
		rep.Estado = CensoParcial
	case rep.Ajenos > 0:
		rep.Estado = CensoIncompleta
	default:
		rep.Estado = CensoCompleta
	}
	return rep
}

func unaLinea(err error) string {
	return strings.ReplaceAll(err.Error(), "\n", "; ")
}

// esSuperficie reports whether a running container belongs to the campaign, by image or by name.
func esSuperficie(image string, names []string) bool {
	if strings.HasPrefix(image, "opensuperficies-") || strings.HasPrefix(image, "srf-") {
		return true
	}
	for _, n := range names {
		n = strings.TrimPrefix(n, "/")
		if strings.HasPrefix(n, "srf-") || strings.HasPrefix(n, "newton-srf-") {
			return true
		}
	}
	return false
}

// censarDocker lists the running containers through the Docker Engine API. It returns a
// non-nil error unless the API answered 200 with a JSON array that parsed: a dead socket,
// a refused connection, a non-2xx answer, a timeout or a body that is not an array all
// mean the leg did NOT run, never "zero containers".
func censarDocker(ctx context.Context, opts CensoOpciones, med conjuntoMediado, declarados map[string]bool) (int, []string, error) {
	ajenos := []string{}

	cli, err := dockerapi.NewClient(opts.DockerSocket)
	if err != nil {
		return 0, ajenos, fmt.Errorf("client: %w", err)
	}
	ctx, cancel := context.WithTimeout(ctx, opts.TimeoutDocker)
	defer cancel()

	body, err := cli.InspectRaw(ctx, "/containers/json?all=0")
	if err != nil {
		return 0, ajenos, fmt.Errorf("list containers via %s: %w", cli.SocketPath(), err)
	}
	if !bytes.HasPrefix(bytes.TrimSpace(body), []byte("[")) {
		return 0, ajenos, fmt.Errorf("list containers via %s: body is not a JSON array", cli.SocketPath())
	}
	var lista []struct {
		ID    string   `json:"Id"`
		Names []string `json:"Names"`
		Image string   `json:"Image"`
	}
	if err := json.Unmarshal(body, &lista); err != nil {
		return 0, ajenos, fmt.Errorf("list containers via %s: %w", cli.SocketPath(), err)
	}

	observadas := 0
	for _, c := range lista {
		if !esSuperficie(c.Image, c.Names) {
			continue
		}
		observadas++

		mediado := false
		for _, n := range c.Names {
			n = strings.TrimPrefix(n, "/")
			if med.contenedores[n] || declarados[n] {
				mediado = true
				break
			}
		}
		if !mediado {
			nombre := c.ID
			if len(c.Names) > 0 {
				nombre = strings.TrimPrefix(c.Names[0], "/")
			}
			ajenos = append(ajenos, nombre)
		}
	}
	sort.Strings(ajenos)
	return observadas, ajenos, nil
}

type resultadoProcfs struct {
	qemuObservadas    int
	procesosAjenos    []int
	puertosObservados int
	puertosAjenos     []int
	errs              []error
}

// censarProcfs enumerates processes and listening ports under opts.ProcDir. Its errs are
// empty only if: the process directory was listed AND this very process was among the
// entries (a positive control: an empty or wrong procfs cannot pass), every comm file that
// still existed was read, /proc/net/tcp parsed, and /proc/net/tcp6 parsed (or the kernel has no IPv6).
func censarProcfs(opts CensoOpciones, med conjuntoMediado, declarados map[int]bool) resultadoProcfs {
	res := resultadoProcfs{procesosAjenos: []int{}, puertosAjenos: []int{}}

	entradas, err := os.ReadDir(opts.ProcDir)
	if err != nil {
		res.errs = append(res.errs, fmt.Errorf("list %s: %w", opts.ProcDir, err))
	} else {
		vistoYo := false
		for _, e := range entradas {
			pid, err := strconv.Atoi(e.Name())
			if err != nil || pid <= 0 {
				continue
			}
			if pid == opts.SelfPID {
				vistoYo = true
			}
			comm, err := os.ReadFile(filepath.Join(opts.ProcDir, e.Name(), "comm"))
			if err != nil {
				if errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ESRCH) {
					continue // the process exited between the listing and the read
				}
				res.errs = append(res.errs, fmt.Errorf("read comm of pid %d: %w", pid, err))
				continue
			}
			nombre := strings.TrimSpace(string(comm))
			if nombre == "qemu-system-aar" || nombre == "qemu-system-aarch64" {
				res.qemuObservadas++
				if !med.pids[pid] {
					res.procesosAjenos = append(res.procesosAjenos, pid)
				}
			}
		}
		if !vistoYo {
			res.errs = append(res.errs, fmt.Errorf("positive control failed: this process (pid %d) is not listed under %s", opts.SelfPID, opts.ProcDir))
		}
	}

	vistos := map[int]bool{}
	for _, tabla := range []string{"tcp", "tcp6"} {
		ruta := filepath.Join(opts.ProcDir, "net", tabla)
		f, err := os.Open(ruta)
		if err != nil {
			if tabla == "tcp6" && errors.Is(err, fs.ErrNotExist) {
				if _, e2 := os.Stat(filepath.Join(opts.ProcDir, "net", "if_inet6")); errors.Is(e2, fs.ErrNotExist) {
					continue // the kernel has no IPv6 stack: there is no second table to read
				}
			}
			res.errs = append(res.errs, fmt.Errorf("open %s: %w", ruta, err))
			continue
		}
		puertos, err := puertosEnEscucha(f)
		f.Close()
		if err != nil {
			res.errs = append(res.errs, fmt.Errorf("parse %s: %w", ruta, err))
			continue
		}
		for _, p := range puertos {
			if !puertoDeCampana(p) || vistos[p] {
				continue
			}
			vistos[p] = true
			res.puertosObservados++
			if !med.puertos[p] && !declarados[p] {
				res.puertosAjenos = append(res.puertosAjenos, p)
			}
		}
	}
	sort.Ints(res.procesosAjenos)
	sort.Ints(res.puertosAjenos)
	return res
}

func puertoDeCampana(p int) bool {
	return (p >= puertoXpraMin && p <= puertoXpraMax) || (p >= puertoCDPMin && p <= puertoCDPMax)
}

// puertosEnEscucha parses a /proc/net/tcp(6) table and returns every port in TCP_LISTEN state.
// A table whose header is missing or whose row cannot be parsed is an error, not an empty answer.
func puertosEnEscucha(r io.Reader) ([]int, error) {
	sc := bufio.NewScanner(r)
	if !sc.Scan() {
		if err := sc.Err(); err != nil {
			return nil, err
		}
		return nil, errors.New("empty table")
	}
	if !strings.Contains(sc.Text(), "local_address") {
		return nil, fmt.Errorf("unexpected header %q", sc.Text())
	}
	var puertos []int
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) == 0 {
			continue
		}
		if len(fields) < 4 {
			return nil, fmt.Errorf("malformed row %q", sc.Text())
		}
		if fields[3] != "0A" { // 0A = TCP_LISTEN
			continue
		}
		i := strings.LastIndexByte(fields[1], ':')
		if i < 0 {
			return nil, fmt.Errorf("malformed address %q", fields[1])
		}
		p, err := strconv.ParseUint(fields[1][i+1:], 16, 16)
		if err != nil {
			return nil, fmt.Errorf("malformed port in %q: %w", fields[1], err)
		}
		puertos = append(puertos, int(p))
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return puertos, nil
}
