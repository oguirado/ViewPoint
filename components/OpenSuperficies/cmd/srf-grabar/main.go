package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"

	"opensuperficies.local/srf/src/surfacebook/grabador"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: srf-grabar <subcommand> [options]\navailable subcommands: prueba-anidada, objetivo, grabar\n")
		os.Exit(2)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	var code int
	switch subcmd := os.Args[1]; subcmd {
	case "prueba-anidada":
		code = cmdPruebaAnidada(ctx, os.Args[2:])
	case "objetivo":
		code = cmdObjetivo(os.Args[2:])
	case "grabar":
		cancel() // grabar owns its signals: SIGINT is the clean stop, SIGTERM the abort
		code = cmdGrabar(os.Args[2:])
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q. Subcommands: prueba-anidada, objetivo, grabar\n", subcmd)
		code = 2
	}
	cancel()
	os.Exit(code)
}

func imprimirJSON(v any) {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "error serializing json: %v\n", err)
		return
	}
	fmt.Println(string(data))
}

// cmdPruebaAnidada runs the nested recording test. The report is printed on the failing paths too,
// with the complete field set; the exit code is 0 only when the session is OK.
func cmdPruebaAnidada(ctx context.Context, args []string) int {
	fs := flag.NewFlagSet("prueba-anidada", flag.ExitOnError)
	displayFlag := fs.String("display", ":150", "X11 nested display target")
	segundosFlag := fs.Int("segundos", 3, "Duration of the recording in seconds")
	raizFlag := fs.String("raiz", "videos/srf-ng", "Root directory for recording sessions")
	jsonFlag := fs.Bool("json", true, "Emit JSON summary (default true)")
	_ = fs.Parse(args)

	res, err := grabador.PruebaAnidada(ctx, *displayFlag, *segundosFlag, *raizFlag)
	if res == nil {
		res = &grabador.SesionResultado{DisplaySolicitado: *displayFlag, ManifiestoEstado: "ausente", Error: fmt.Sprint(err)}
	}

	if *jsonFlag {
		imprimirJSON(res)
	} else if err != nil {
		fmt.Fprintf(os.Stderr, "prueba-anidada error: %v\n", err)
	} else {
		fmt.Printf("session %s finished with state %s (%d ms)\n", res.SessionID, res.ManifiestoEstado, res.DuracionMS)
	}

	if err != nil || !res.OK || res.ManifiestoEstado != "completa" {
		return 1
	}
	return 0
}

// cmdObjetivo selects the recording target: from a candidate file (--candidatos) or from the X
// server of a nested display (--display, optionally --pid). Ties fail closed.
func cmdObjetivo(args []string) int {
	fs := flag.NewFlagSet("objetivo", flag.ExitOnError)
	candidatosFlag := fs.String("candidatos", "", "Path to JSON candidate fixture")
	displayFlag := fs.String("display", "", "Nested display whose X server is asked for the candidates")
	pidFlag := fs.Int("pid", 0, "With --display: only windows that _NET_WM_PID assigns to this pid")
	jsonFlag := fs.Bool("json", true, "Emit JSON summary (default true)")
	_ = fs.Parse(args)

	if (*candidatosFlag == "") == (*displayFlag == "") {
		fmt.Fprintf(os.Stderr, "objetivo needs exactly one of --candidatos <file.json> or --display <:N>\n")
		return 2
	}

	var cands []grabador.Candidato
	var err error
	if *candidatosFlag != "" {
		cands, err = grabador.CargarCandidatosArchivo(*candidatosFlag)
		if err != nil {
			err = fmt.Errorf("error loading candidates: %w", err)
		}
	} else {
		cands, err = grabador.CandidatosDeDisplay(*displayFlag, *pidFlag)
	}
	if err != nil {
		codigo := "grabador_candidatos_no_disponibles"
		switch {
		case errors.Is(err, grabador.ErrDisplayProhibido):
			codigo = "grabador_display_prohibido"
		case errors.Is(err, grabador.ErrDisplayInvalido):
			codigo = "grabador_display_invalido"
		}
		fallo(*jsonFlag, codigo, err)
		return 1
	}

	elegido, err := grabador.ElegirHost(cands)
	if err != nil {
		errCode := "error_desconocido"
		switch {
		case errors.Is(err, grabador.ErrObjetivoAmbiguo):
			errCode = "grabador_objetivo_ambiguo"
		case errors.Is(err, grabador.ErrSinObjetivo):
			errCode = "grabador_sin_objetivo"
		}
		fallo(*jsonFlag, errCode, err)
		return 1
	}

	if *jsonFlag {
		imprimirJSON(map[string]any{
			"ok":     true,
			"xid":    grabador.Hex(elegido.XID),
			"nombre": elegido.Nombre,
			"clase":  elegido.Clase,
			"area":   elegido.Area(),
		})
	} else {
		fmt.Println(grabador.Hex(elegido.XID))
	}
	return 0
}

func fallo(json bool, codigo string, err error) {
	if json {
		imprimirJSON(map[string]any{"ok": false, "error": codigo, "detalle": err.Error(), "xid": nil})
	} else {
		fmt.Fprintf(os.Stderr, "error: %s (%v)\n", codigo, err)
	}
}

// imprimirLinea writes one JSON object on one line: the supervisor (cockpit/grabacion.go) reads stdout line by line.
func imprimirLinea(v any) {
	data, err := json.Marshal(v)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error serializing json: %v\n", err)
		return
	}
	fmt.Println(string(data))
}

// parsearXID reads a window id as srf-grabar prints it (0x…) or as a decimal; "" is 0 (choose on the display).
func parsearXID(texto string) (uint64, error) {
	texto = strings.TrimSpace(texto)
	if texto == "" {
		return 0, nil
	}
	if strings.HasPrefix(texto, "0x") || strings.HasPrefix(texto, "0X") {
		return strconv.ParseUint(texto[2:], 16, 64)
	}
	return strconv.ParseUint(texto, 10, 64)
}

// cmdGrabar records a nested display for the cockpit until it is told to stop. Two lines reach stdout: at t0
// {"evento":"abierta",…} with the session id and directory, and at the end the complete SesionResultado. SIGINT is
// the operator's clean stop (the manifest may reach "completa"); SIGTERM, or a second SIGINT, cancels the session.
// --segundos > 0 bounds the recording as prueba-anidada does; 0 records until the stop signal.
func cmdGrabar(args []string) int {
	fs := flag.NewFlagSet("grabar", flag.ExitOnError)
	displayFlag := fs.String("display", ":150", "Nested display to record (the operator's host displays are refused)")
	xidFlag := fs.String("xid", "", "Target window, 0x… or decimal; empty: the largest mapped window of the display (ties fail closed)")
	raizFlag := fs.String("raiz", "videos/srf-ng", "Root directory for recording sessions")
	tituloFlag := fs.String("titulo", "surface-recording", "Session title (part of the session id)")
	tamanoFlag := fs.String("tamano", "", "Recorded area WxH (default 1024x768)")
	segundosFlag := fs.Int("segundos", 0, "Duration in seconds; 0 records until SIGINT")
	_ = fs.Parse(args)

	xid, err := parsearXID(*xidFlag)
	if err != nil {
		fallo(true, "grabador_xid_invalido", err)
		return 2
	}
	if xid == 0 {
		cands, err := grabador.CandidatosDeDisplay(*displayFlag, 0)
		if err != nil {
			fallo(true, "grabador_candidatos_no_disponibles", err)
			return 1
		}
		elegido, err := grabador.ElegirHost(cands)
		if err != nil {
			codigo := "grabador_sin_objetivo"
			if errors.Is(err, grabador.ErrObjetivoAmbiguo) {
				codigo = "grabador_objetivo_ambiguo"
			}
			fallo(true, codigo, err)
			return 1
		}
		xid = elegido.XID
	}

	parar := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	senales := make(chan os.Signal, 2)
	signal.Notify(senales, os.Interrupt, syscall.SIGTERM)
	go func() {
		parado := false
		for sig := range senales {
			if sig == os.Interrupt && !parado {
				parado = true
				close(parar)
				continue
			}
			cancel()
		}
	}()

	res, err := grabador.EjecutarSesion(ctx, grabador.SesionOpciones{
		Display:  *displayFlag,
		Raiz:     *raizFlag,
		Titulo:   *tituloFlag,
		Segundos: *segundosFlag,
		XID:      xid,
		Tamano:   *tamanoFlag,
		Parar:    parar,
		AlAbrir: func(id, dir string) {
			imprimirLinea(map[string]any{"evento": "abierta", "session_id": id, "dir": dir, "xid": grabador.Hex(xid), "display": *displayFlag})
		},
	})
	signal.Stop(senales)
	if res == nil {
		res = &grabador.SesionResultado{DisplaySolicitado: *displayFlag, ManifiestoEstado: "ausente", Error: fmt.Sprint(err)}
	}
	imprimirLinea(res)
	if err != nil || !res.OK || res.ManifiestoEstado != "completa" {
		return 1
	}
	return 0
}
