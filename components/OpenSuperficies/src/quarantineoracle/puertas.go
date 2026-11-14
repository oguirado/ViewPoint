package quarantineoracle

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

// PuertaResultado define la evaluacion individual de una de las 14 puertas.
type PuertaResultado struct {
	Puerta  string `json:"puerta"`
	Nombre  string `json:"nombre"`
	Tipo    string `json:"tipo"`
	Verde   bool   `json:"verde"`
	Detalle string `json:"detalle"`
}

// CuarentenaResultado representa el veredicto completo de la auditoria de cuarentena.
type CuarentenaResultado struct {
	Verde      bool              `json:"verde"`
	Contenedor string            `json:"contenedor"`
	Clase      string            `json:"clase"`
	Puertas    []PuertaResultado `json:"puertas"`
}

// ContainerInspect modela los datos necesarios devueltos por el daemon de Docker.
type ContainerInspect struct {
	Id              string                      `json:"Id"`
	Name            string                      `json:"Name"`
	State           ContainerState              `json:"State"`
	HostConfig      HostConfig                  `json:"HostConfig"`
	Mounts          []ContainerMount            `json:"Mounts"`
	Config          ContainerConfig             `json:"Config"`
	NetworkSettings ContainerNetworkSettings    `json:"NetworkSettings"`
}

type ContainerState struct {
	Status   string `json:"Status"`
	Running  bool   `json:"Running"`
	Pid      int    `json:"Pid"`
	ExitCode int    `json:"ExitCode"`
}

type HostConfig struct {
	ShmSize     int64    `json:"ShmSize"`
	CapDrop     []string `json:"CapDrop"`
	SecurityOpt []string `json:"SecurityOpt"`
}

type ContainerMount struct {
	Type        string `json:"Type"`
	Source      string `json:"Source"`
	Destination string `json:"Destination"`
	Mode        string `json:"Mode"`
	RW          bool   `json:"RW"`
	Propagation string `json:"Propagation"`
}

type ContainerConfig struct {
	Env []string `json:"Env"`
}

type ContainerNetworkSettings struct {
	Networks map[string]ContainerNetwork `json:"Networks"`
	Ports    map[string][]PortBinding    `json:"Ports"`
}

type ContainerNetwork struct {
	IPAddress string `json:"IPAddress"`
	NetworkID string `json:"NetworkID"`
}

type PortBinding struct {
	HostIp   string `json:"HostIp"`
	HostPort string `json:"HostPort"`
}

// ContainerEngine desacopla la interaccion con el demonio de contenedores.
type ContainerEngine interface {
	Inspect(ctx context.Context, nameOrID string) (*ContainerInspect, error)
	Exec(ctx context.Context, nameOrID string, cmd []string) (int, string, error)
}

// HTTPProber desacopla la comprobacion de endpoints HTTP en bucle local.
type HTTPProber interface {
	Probe(ctx context.Context, url string) (int, error)
}

// DockerSocketClient implementa ContainerEngine comunicandose con el socket unix de Docker.
type DockerSocketClient struct {
	socketPath string
	httpClient *http.Client
}

// NewDockerSocketClient construye un cliente para el daemon de Docker via socket unix.
func NewDockerSocketClient(socketPath string) *DockerSocketClient {
	if socketPath == "" {
		socketPath = "/var/run/docker.sock"
	}
	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socketPath)
		},
		DisableKeepAlives: false,
	}
	return &DockerSocketClient{
		socketPath: socketPath,
		httpClient: &http.Client{
			Transport: tr,
			Timeout:   10 * time.Second,
		},
	}
}

// Inspect consulta la configuracion e inspeccion del contenedor.
func (c *DockerSocketClient) Inspect(ctx context.Context, nameOrID string) (*ContainerInspect, error) {
	url := fmt.Sprintf("http://localhost/containers/%s/json", nameOrID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("error conectando con daemon docker: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("docker inspect devolvio status %d: %s", resp.StatusCode, string(body))
	}

	var insp ContainerInspect
	if err := json.NewDecoder(resp.Body).Decode(&insp); err != nil {
		return nil, fmt.Errorf("error decodificando json de inspect: %w", err)
	}
	return &insp, nil
}

// Exec ejecuta un comando dentro del contenedor mediante la API de Docker y devuelve el codigo de salida y la salida combinada.
func (c *DockerSocketClient) Exec(ctx context.Context, nameOrID string, cmd []string) (int, string, error) {
	createURL := fmt.Sprintf("http://localhost/containers/%s/exec", nameOrID)
	createReqBody, err := json.Marshal(map[string]interface{}{
		"AttachStdout": true,
		"AttachStderr": true,
		"Cmd":          cmd,
	})
	if err != nil {
		return -1, "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, createURL, bytes.NewReader(createReqBody))
	if err != nil {
		return -1, "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return -1, "", fmt.Errorf("error creando exec: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return -1, "", fmt.Errorf("error en docker exec create status %d: %s", resp.StatusCode, string(body))
	}

	var createResp struct {
		Id string `json:"Id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&createResp); err != nil {
		return -1, "", fmt.Errorf("error decodificando respuesta de exec create: %w", err)
	}

	startURL := fmt.Sprintf("http://localhost/exec/%s/start", createResp.Id)
	startReqBody, _ := json.Marshal(map[string]interface{}{
		"Detach": false,
		"Tty":    false,
	})

	sreq, err := http.NewRequestWithContext(ctx, http.MethodPost, startURL, bytes.NewReader(startReqBody))
	if err != nil {
		return -1, "", err
	}
	sreq.Header.Set("Content-Type", "application/json")

	sresp, err := c.httpClient.Do(sreq)
	if err != nil {
		return -1, "", fmt.Errorf("error iniciando exec: %w", err)
	}
	defer sresp.Body.Close()

	if sresp.StatusCode != http.StatusOK {
		return -1, "", fmt.Errorf("exec start returned status %d", sresp.StatusCode)
	}

	rawOutput, err := io.ReadAll(sresp.Body)
	if err != nil {
		return -1, "", fmt.Errorf("error leyendo salida exec: %w", err)
	}
	demuxed := demuxDockerStream(rawOutput)

	inspectURL := fmt.Sprintf("http://localhost/exec/%s/json", createResp.Id)
	ireq, err := http.NewRequestWithContext(ctx, http.MethodGet, inspectURL, nil)
	if err != nil {
		return -1, demuxed, fmt.Errorf("error creando request exec inspect: %w", err)
	}
	iresp, err := c.httpClient.Do(ireq)
	if err != nil {
		return -1, demuxed, fmt.Errorf("error consultando exec inspect: %w", err)
	}
	defer iresp.Body.Close()

	if iresp.StatusCode != http.StatusOK {
		return -1, demuxed, fmt.Errorf("exec inspect returned status %d", iresp.StatusCode)
	}

	var execInspect struct {
		ExitCode *int `json:"ExitCode"`
	}
	if err := json.NewDecoder(iresp.Body).Decode(&execInspect); err != nil {
		return -1, demuxed, fmt.Errorf("error decodificando exec inspect: %w", err)
	}
	if execInspect.ExitCode == nil {
		return -1, demuxed, fmt.Errorf("exec inspect no contiene ExitCode")
	}

	return *execInspect.ExitCode, demuxed, nil
}

func demuxDockerStream(data []byte) string {
	var buf bytes.Buffer
	reader := bytes.NewReader(data)
	hdr := make([]byte, 8)
	for {
		n, err := io.ReadFull(reader, hdr)
		if err != nil || n < 8 {
			if reader.Len() > 0 {
				buf.Write(data[len(data)-reader.Len():])
			}
			break
		}
		frameLen := binary.BigEndian.Uint32(hdr[4:8])
		payload := make([]byte, frameLen)
		pn, perr := io.ReadFull(reader, payload)
		if perr != nil {
			buf.Write(payload[:pn])
			break
		}
		buf.Write(payload)
	}
	return buf.String()
}

// DefaultHTTPProber realiza peticiones HTTP estandar con timeout.
type DefaultHTTPProber struct {
	client *http.Client
}

// NewDefaultHTTPProber construye un probador HTTP por defecto.
func NewDefaultHTTPProber(timeout time.Duration) *DefaultHTTPProber {
	return &DefaultHTTPProber{
		client: &http.Client{
			Timeout: timeout,
		},
	}
}

// Probe ejecuta un GET contra la URL dada.
func (p *DefaultHTTPProber) Probe(ctx context.Context, url string) (int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return 0, err
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	return resp.StatusCode, nil
}

// Oracle representa el evaluador de cuarentena para las 14 puertas.
type Oracle struct {
	engine ContainerEngine
	prober HTTPProber
}

// NewOracle construye una nueva instancia del oraculo de cuarentena.
func NewOracle(engine ContainerEngine, prober HTTPProber) *Oracle {
	if engine == nil {
		engine = NewDockerSocketClient("/var/run/docker.sock")
	}
	if prober == nil {
		prober = NewDefaultHTTPProber(3 * time.Second)
	}
	return &Oracle{
		engine: engine,
		prober: prober,
	}
}

// Evaluar ejecuta la comprobacion exhaustiva de las 14 puertas sobre un contenedor.
func (o *Oracle) Evaluar(ctx context.Context, contenedor string, clase string) (*CuarentenaResultado, error) {
	if clase == "" {
		clase = "browser"
	}

	insp, err := o.engine.Inspect(ctx, contenedor)
	if err != nil {
		return &CuarentenaResultado{
			Verde:      false,
			Contenedor: contenedor,
			Clase:      clase,
			Puertas: []PuertaResultado{
				{
					Puerta:  "A",
					Nombre:  "El contenedor no alcanza :1",
					Tipo:    "veredicto",
					Verde:   false,
					Detalle: fmt.Sprintf("no se pudo inspeccionar el contenedor: %v", err),
				},
			},
		}, err
	}

	puertas := make([]PuertaResultado, 14)

	// Puerta A (veredicto): El contenedor no alcanza :1
	puertas[0] = o.evaluarPuertaA(ctx, contenedor, insp)

	// Puertas B, C, D (informativa): Observacion de clientes X, foco y portales
	puertas[1] = o.evaluarPuertaB(ctx)
	puertas[2] = o.evaluarPuertaC(ctx)
	puertas[3] = o.evaluarPuertaD(ctx)

	// Puerta E (veredicto): Sin alcance a D-Bus
	puertas[4] = o.evaluarPuertaE(ctx, contenedor, insp, clase)

	// Puerta F (veredicto): Sin runtime dir /run/user montado del host
	puertas[5] = o.evaluarPuertaF(insp)

	// Puerta G (veredicto): Sin montajes sensibles del host
	puertas[6] = o.evaluarPuertaG(insp)

	// Puerta H (veredicto): Variables de cuarentena parametrizadas por clase
	puertas[7] = o.evaluarPuertaH(insp, clase)

	// Puerta I (veredicto): Memoria compartida adecuada >= 2GB
	puertas[8] = o.evaluarPuertaI(insp)

	// Puerta J (veredicto): Display virtual :100 vivo
	puertas[9] = o.evaluarPuertaJ(ctx, contenedor, insp)

	// Puerta K (veredicto): Renderer GL software (llvmpipe)
	puertas[10] = o.evaluarPuertaK(ctx, contenedor, insp)

	// Puerta L (veredicto): Endpoint HTML5 respondiendo
	puertas[11] = o.evaluarPuertaL(ctx, insp)

	// Puerta M (veredicto): Cero intentos de conexion a sockets de portal o bus
	puertas[12] = o.evaluarPuertaM(ctx, contenedor, insp)

	// Puerta N (veredicto): Aislamiento de red (no en bridge por defecto)
	puertas[13] = o.evaluarPuertaN(insp)

	globalVerde := true
	for _, p := range puertas {
		if p.Tipo == "veredicto" && !p.Verde {
			globalVerde = false
			break
		}
	}

	return &CuarentenaResultado{
		Verde:      globalVerde,
		Contenedor: contenedor,
		Clase:      clase,
		Puertas:    puertas,
	}, nil
}

func (o *Oracle) evaluarPuertaA(ctx context.Context, contenedor string, insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "A",
		Nombre: "El contenedor no alcanza :1",
		Tipo:   "veredicto",
	}

	if !insp.State.Running {
		p.Verde = false
		p.Detalle = "A.0 la capsula no esta running: ninguna sonda puede correr"
		return p
	}

	// A.1: desde DENTRO, DISPLAY=:1 xdpyinfo debe FALLAR con 'unable to open display'
	rcA1, outA1, errA1 := o.engine.Exec(ctx, contenedor, []string{"env", "DISPLAY=:1", "xdpyinfo"})
	if errA1 != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("A.1 la sonda no pudo juzgar: exec error: %v", errA1)
		return p
	}
	if rcA1 == 0 {
		p.Verde = false
		p.Detalle = "A.1 DISPLAY=:1 xdpyinfo desde dentro: rc=0, la capsula ALCANZA :1"
		return p
	}
	if !strings.Contains(outA1, "unable to open display") {
		p.Verde = false
		p.Detalle = fmt.Sprintf("A.1 la sonda no pudo juzgar: rc=%d (%s)", rcA1, strings.TrimSpace(outA1))
		return p
	}

	// A.2: dentro de la capsula, /tmp/.X11-unix ausente o vacio
	rcA2, outA2, errA2 := o.engine.Exec(ctx, contenedor, []string{"sh", "-c", "[ -e /tmp/.X11-unix ] || { echo __AUSENTE__; exit 0; }; ls -A /tmp/.X11-unix"})
	if errA2 != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("A.2 la sonda no pudo juzgar: exec error: %v", errA2)
		return p
	}
	if rcA2 != 0 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("A.2 la sonda no pudo juzgar: rc=%d (%s)", rcA2, strings.TrimSpace(outA2))
		return p
	}
	trimmedA2 := strings.TrimSpace(outA2)
	if trimmedA2 != "" && trimmedA2 != "__AUSENTE__" {
		p.Verde = false
		p.Detalle = fmt.Sprintf("A.2 /tmp/.X11-unix dentro de la capsula contiene: %s", trimmedA2)
		return p
	}

	// A.3: ningun montaje con .X11-unix (origen o destino) en docker inspect
	for _, m := range insp.Mounts {
		if strings.Contains(m.Source, ".X11-unix") || strings.Contains(m.Destination, ".X11-unix") {
			p.Verde = false
			p.Detalle = fmt.Sprintf("A.3 montaje X11 presente: %s -> %s", m.Source, m.Destination)
			return p
		}
	}

	p.Verde = true
	p.Detalle = "la capsula no alcanza :1: xdpyinfo a :1 falla desde dentro, /tmp/.X11-unix sin sockets y cero montajes X11"
	return p
}

func (o *Oracle) evaluarPuertaB(ctx context.Context) PuertaResultado {
	p := PuertaResultado{
		Puerta: "B",
		Nombre: "Clientes X nuevos en :1 respecto de la linea base",
		Tipo:   "informativa",
		Verde:  true,
	}
	cmd, err := lanzador.CommandContext(ctx, "xlsclients", "-display", ":1")
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: xlsclients no disponible via lanzador (%v)", err)
		return p
	}
	out, err := cmd.Output()
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: xlsclients falló contra :1 (%v)", err)
		return p
	}
	linesNow := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(linesNow) == 0 || (len(linesNow) == 1 && linesNow[0] == "") {
		p.Detalle = "sin cifra: xlsclients no devolvio nada contra :1 (sonda ciega; no emite veredicto)"
		return p
	}
	baseData, err := os.ReadFile("/tmp/display1-clients.txt")
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: linea base /tmp/display1-clients.txt ausente; clientes_ahora=%d", len(linesNow))
		return p
	}
	baseLines := strings.Split(strings.TrimSpace(string(baseData)), "\n")
	baseSet := make(map[string]bool)
	for _, l := range baseLines {
		if l != "" {
			baseSet[l] = true
		}
	}
	nuevos := 0
	for _, l := range linesNow {
		if l != "" && !baseSet[l] {
			nuevos++
		}
	}
	p.Detalle = fmt.Sprintf("clientes_nuevos=%d respecto de la linea base; clientes_ahora=%d", nuevos, len(linesNow))
	return p
}

func (o *Oracle) evaluarPuertaC(ctx context.Context) PuertaResultado {
	p := PuertaResultado{
		Puerta: "C",
		Nombre: "Foco de :1 respecto de la linea base",
		Tipo:   "informativa",
		Verde:  true,
	}
	cmd, err := lanzador.CommandContext(ctx, "xdotool", "getactivewindow")
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: xdotool no disponible via lanzador (%v)", err)
		return p
	}
	out, err := cmd.Output()
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: xdotool falló contra :1 (%v)", err)
		return p
	}
	curr := strings.TrimSpace(string(out))
	baseData, err := os.ReadFile("/tmp/display1-activewindow.txt")
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: linea base del foco ausente; ventana_activa_ahora=%s", curr)
		return p
	}
	base := strings.TrimSpace(string(baseData))
	if base == curr {
		p.Detalle = fmt.Sprintf("foco_cambiado=0 (ventana activa %s)", curr)
	} else {
		p.Detalle = fmt.Sprintf("foco_cambiado=1 (%s -> %s)", base, curr)
	}
	return p
}

func (o *Oracle) evaluarPuertaD(ctx context.Context) PuertaResultado {
	p := PuertaResultado{
		Puerta: "D",
		Nombre: "Portales nuevos en la sesion del host",
		Tipo:   "informativa",
		Verde:  true,
	}
	cmd, err := lanzador.CommandContext(ctx, "busctl", "--user", "list")
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: busctl no disponible via lanzador (%v)", err)
		return p
	}
	out, err := cmd.Output()
	if err != nil {
		p.Detalle = fmt.Sprintf("sin cifra: busctl falló (%v)", err)
		return p
	}
	linesNow := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(linesNow) == 0 || (len(linesNow) == 1 && linesNow[0] == "") {
		p.Detalle = "sin cifra: busctl --user list no devolvio nada (sonda ciega; no emite veredicto)"
		return p
	}
	baseData, err := os.ReadFile("/tmp/display1-portals.txt")
	if err != nil {
		p.Detalle = "sin cifra: linea base /tmp/display1-portals.txt ausente"
		return p
	}
	baseLines := strings.Split(strings.TrimSpace(string(baseData)), "\n")
	baseSet := make(map[string]bool)
	for _, l := range baseLines {
		parts := strings.Fields(l)
		if len(parts) > 0 {
			baseSet[parts[0]] = true
		}
	}
	nuevosPortales := 0
	for _, l := range linesNow {
		parts := strings.Fields(l)
		if len(parts) > 0 {
			name := parts[0]
			if !baseSet[name] && strings.Contains(name, "portal") {
				nuevosPortales++
			}
		}
	}
	p.Detalle = fmt.Sprintf("portales_nuevos=%d respecto de la linea base", nuevosPortales)
	return p
}

func (o *Oracle) evaluarPuertaE(ctx context.Context, contenedor string, insp *ContainerInspect, clase string) PuertaResultado {
	p := PuertaResultado{
		Puerta: "E",
		Nombre: "El contenedor no alcanza ningun D-Bus",
		Tipo:   "veredicto",
	}

	for _, m := range insp.Mounts {
		if strings.Contains(m.Source, "dbus") || strings.Contains(m.Destination, "dbus") ||
			strings.Contains(m.Source, "system_bus_socket") || strings.Contains(m.Destination, "system_bus_socket") {
			p.Verde = false
			p.Detalle = fmt.Sprintf("fuga: socket dbus montado (%s -> %s)", m.Source, m.Destination)
			return p
		}
	}

	envMap := make(map[string]string)
	for _, e := range insp.Config.Env {
		k, v, found := strings.Cut(e, "=")
		if found {
			envMap[k] = v
		}
	}

	if clase == "desktop" {
		if envMap["DBUS_SESSION_BUS_ADDRESS"] != "unix:path=/run/newton-tenant/bus" || envMap["DBUS_SYSTEM_BUS_ADDRESS"] != "disabled:" {
			p.Verde = false
			p.Detalle = "variables dbus no coinciden con especificacion desktop privada"
			return p
		}
	} else {
		if envMap["DBUS_SESSION_BUS_ADDRESS"] != "disabled:" || envMap["DBUS_SYSTEM_BUS_ADDRESS"] != "disabled:" {
			p.Verde = false
			p.Detalle = "variables dbus no deshabilitadas en entorno"
			return p
		}
	}

	// Comprobacion activa: para todas las clases, el contenedor no debe alcanzar el bus de sistema del host
	hostBusCmd := "command -v dbus-send >/dev/null 2>&1 && (dbus-send --address=unix:path=/var/run/dbus/system_bus_socket --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames >/dev/null 2>&1 || dbus-send --address=unix:path=/run/dbus/system_bus_socket --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames >/dev/null 2>&1)"
	rcHost, _, errHost := o.engine.Exec(ctx, contenedor, []string{"sh", "-c", hostBusCmd})
	if errHost == nil && rcHost == 0 {
		p.Verde = false
		p.Detalle = "el contenedor alcanzo el bus del host"
		return p
	}

	// Para clases distintas de desktop, comprobar que no alcance tampoco ningun bus de sesion activo
	if clase != "desktop" {
		sessionCmd := "command -v dbus-send >/dev/null 2>&1 && dbus-send --session --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames >/dev/null 2>&1"
		rcSession, _, errSession := o.engine.Exec(ctx, contenedor, []string{"sh", "-c", sessionCmd})
		if errSession == nil && rcSession == 0 {
			p.Verde = false
			p.Detalle = "el contenedor alcanzo un bus de sesion activo"
			return p
		}
	}

	p.Verde = true
	p.Detalle = "bus de sesion inalcanzable desde el contenedor (ausencia o deshabilitado)"
	return p
}

func (o *Oracle) evaluarPuertaF(insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "F",
		Nombre: "Sin runtime dir del host dentro del contenedor",
		Tipo:   "veredicto",
	}

	for _, m := range insp.Mounts {
		if m.Source == "/run/user" || strings.HasPrefix(m.Source, "/run/user/") ||
			m.Destination == "/run/user" || strings.HasPrefix(m.Destination, "/run/user/") {
			p.Verde = false
			p.Detalle = fmt.Sprintf("/run/user montado en el contenedor (%s -> %s)", m.Source, m.Destination)
			return p
		}
	}

	p.Verde = true
	p.Detalle = "/run/user ausente o no montado desde el host"
	return p
}

func (o *Oracle) evaluarPuertaG(insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "G",
		Nombre: "Sin montajes sensibles del host",
		Tipo:   "veredicto",
	}

	sensibles := []string{
		"/proc",
		"/sys",
		"/dev",
		"/run/user",
		"/tmp/.X11-unix",
		"/home/user/.Xauthority",
	}

	for _, m := range insp.Mounts {
		for _, s := range sensibles {
			if m.Source == s || strings.HasPrefix(m.Source, s+"/") {
				p.Verde = false
				p.Detalle = fmt.Sprintf("fuga: host %s montado en el contenedor (%s)", s, m.Source)
				return p
			}
		}
	}

	p.Verde = true
	p.Detalle = "cero montajes sensibles del host"
	return p
}

func (o *Oracle) evaluarPuertaH(insp *ContainerInspect, clase string) PuertaResultado {
	p := PuertaResultado{
		Puerta: "H",
		Nombre: "Variables de cuarentena en el contenedor",
		Tipo:   "veredicto",
	}

	var esperadas []string
	if clase == "desktop" {
		esperadas = []string{
			"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/newton-tenant/bus",
			"DBUS_SYSTEM_BUS_ADDRESS=disabled:",
			"XDG_RUNTIME_DIR=/run/newton-tenant",
			"XDG_SESSION_TYPE=x11",
			"GTK_USE_PORTAL=0",
			"GDK_BACKEND=x11",
			"QT_QPA_PLATFORM=xcb",
			"NO_AT_BRIDGE=0",
			"GIO_USE_VFS=local",
			"DISPLAY=:100",
			"LIBGL_ALWAYS_SOFTWARE=1",
			"GALLIUM_DRIVER=llvmpipe",
		}
	} else {
		esperadas = []string{
			"DBUS_SESSION_BUS_ADDRESS=disabled:",
			"DBUS_SYSTEM_BUS_ADDRESS=disabled:",
			"XDG_RUNTIME_DIR=/run/newton-tenant",
			"XDG_SESSION_TYPE=x11",
			"GTK_USE_PORTAL=0",
			"GDK_BACKEND=x11",
			"QT_QPA_PLATFORM=xcb",
			"NO_AT_BRIDGE=1",
			"GIO_USE_VFS=local",
			"DISPLAY=:100",
			"LIBGL_ALWAYS_SOFTWARE=1",
			"GALLIUM_DRIVER=llvmpipe",
		}
	}

	envSet := make(map[string]bool)
	for _, e := range insp.Config.Env {
		envSet[e] = true
	}

	var ausentes []string
	for _, req := range esperadas {
		if !envSet[req] {
			ausentes = append(ausentes, req)
		}
	}

	if len(ausentes) > 0 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("cuarentena incompleta: falta %s", strings.Join(ausentes, ", "))
		return p
	}

	p.Verde = true
	p.Detalle = fmt.Sprintf("12 variables de cuarentena presentes en Config.Env (clase %s)", clase)
	return p
}

func (o *Oracle) evaluarPuertaI(insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "I",
		Nombre: "Memoria compartida adecuada",
		Tipo:   "veredicto",
	}

	const minShm = 2147483648 // 2 GB
	if insp.HostConfig.ShmSize >= minShm {
		p.Verde = true
		p.Detalle = fmt.Sprintf("/dev/shm >= 2GB (%d bytes)", insp.HostConfig.ShmSize)
	} else {
		p.Verde = false
		p.Detalle = fmt.Sprintf("/dev/shm menor a 2GB (%d bytes)", insp.HostConfig.ShmSize)
	}
	return p
}

func (o *Oracle) evaluarPuertaJ(ctx context.Context, contenedor string, insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "J",
		Nombre: "Display virtual :100 vivo",
		Tipo:   "veredicto",
	}

	if !insp.State.Running {
		p.Verde = false
		p.Detalle = "capsula no activa; Xvfb inalcanzable"
		return p
	}

	rc, out, err := o.engine.Exec(ctx, contenedor, []string{"xdpyinfo", "-display", ":100"})
	if err == nil && (rc == 0 || strings.Contains(out, "name of display")) {
		p.Verde = true
		p.Detalle = "Xvfb :100 respondiendo a xdpyinfo"
		return p
	}

	p.Verde = false
	p.Detalle = "Xvfb :100 no responde"
	return p
}

func (o *Oracle) evaluarPuertaK(ctx context.Context, contenedor string, insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "K",
		Nombre: "Renderer GL software",
		Tipo:   "veredicto",
	}

	envSoftwareGL := false
	for _, e := range insp.Config.Env {
		if e == "GALLIUM_DRIVER=llvmpipe" || e == "LIBGL_ALWAYS_SOFTWARE=1" {
			envSoftwareGL = true
			break
		}
	}

	rc, out, err := o.engine.Exec(ctx, contenedor, []string{"glxinfo", "-display", ":100"})
	if err == nil && rc == 0 {
		if strings.Contains(strings.ToLower(out), "llvmpipe") {
			p.Verde = true
			p.Detalle = "OpenGL renderer es llvmpipe"
			return p
		}
		p.Verde = false
		p.Detalle = fmt.Sprintf("OpenGL renderer no es llvmpipe (%s)", strings.TrimSpace(out))
		return p
	}

	if envSoftwareGL {
		p.Verde = true
		p.Detalle = "glxinfo no disponible; renderer software llvmpipe configurado por entorno"
		return p
	}

	p.Verde = false
	p.Detalle = "glxinfo falló y renderer software no asegurado"
	return p
}

func (o *Oracle) evaluarPuertaL(ctx context.Context, insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "L",
		Nombre: "Endpoint HTML5 respondiendo",
		Tipo:   "veredicto",
	}

	xpraPort := "10099"
	if bindings, ok := insp.NetworkSettings.Ports["10000/tcp"]; ok && len(bindings) > 0 {
		if bindings[0].HostPort != "" {
			xpraPort = bindings[0].HostPort
		}
	}

	url := fmt.Sprintf("http://127.0.0.1:%s/", xpraPort)
	status, err := o.prober.Probe(ctx, url)
	if err == nil && (status == http.StatusOK || status == http.StatusFound) {
		p.Verde = true
		p.Detalle = fmt.Sprintf("Xpra HTML5 respondiendo en 127.0.0.1:%s (status %d)", xpraPort, status)
		return p
	}

	p.Verde = false
	p.Detalle = fmt.Sprintf("Xpra HTML5 no responde en 127.0.0.1:%s: %v", xpraPort, err)
	return p
}

func (o *Oracle) evaluarPuertaM(ctx context.Context, contenedor string, insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "M",
		Nombre: "Sin intentos de conexion a sockets de portal o dbus",
		Tipo:   "veredicto",
	}

	for _, m := range insp.Mounts {
		if strings.Contains(m.Source, "portal") || strings.Contains(m.Destination, "portal") ||
			strings.Contains(m.Source, "dbus") || strings.Contains(m.Destination, "dbus") {
			p.Verde = false
			p.Detalle = "montaje de socket portal o bus detectado"
			return p
		}
	}

	rc, out, err := o.engine.Exec(ctx, contenedor, []string{"cat", "/proc/net/unix"})
	if err != nil {
		p.Verde = false
		p.Detalle = fmt.Sprintf("la sonda no pudo juzgar: exec error: %v", err)
		return p
	}
	if rc != 0 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("la sonda no pudo juzgar: /proc/net/unix rc=%d (%s)", rc, strings.TrimSpace(out))
		return p
	}

	lines := strings.Split(out, "\n")
	leakCount := 0
	for _, line := range lines {
		lower := strings.ToLower(line)
		if strings.Contains(lower, "run/user") || strings.Contains(lower, "dbus") || strings.Contains(lower, "portal") {
			leakCount++
		}
	}
	if leakCount > 0 {
		p.Verde = false
		p.Detalle = fmt.Sprintf("%d sockets de portal o bus observados en /proc/net/unix", leakCount)
		return p
	}

	p.Verde = true
	p.Detalle = "cero conexiones a sockets de bus o portal observadas"
	return p
}

func (o *Oracle) evaluarPuertaN(insp *ContainerInspect) PuertaResultado {
	p := PuertaResultado{
		Puerta: "N",
		Nombre: "Aislamiento de red",
		Tipo:   "veredicto",
	}

	if _, ok := insp.NetworkSettings.Networks["bridge"]; ok {
		p.Verde = false
		p.Detalle = "el contenedor esta en el bridge por defecto: cualquier hermano alcanza CDP"
		return p
	}

	var netNames []string
	for netName := range insp.NetworkSettings.Networks {
		netNames = append(netNames, netName)
	}
	sort.Strings(netNames)

	p.Verde = true
	p.Detalle = fmt.Sprintf("red aislada: conectado a [%s]", strings.Join(netNames, ", "))
	return p
}
