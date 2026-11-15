package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/surfacebook/grabador"
)

func main() {
	os.Exit(run())
}

func run() (code int) {
	displayFlag := flag.String("display", ":150", "X11 display target (default :150)")
	tituloFlag := flag.String("titulo", "Ventana Prueba OpenSuperficies", "Window title")
	anchoFlag := flag.Int("ancho", 800, "Window width in pixels")
	altoFlag := flag.Int("alto", 600, "Window height in pixels")
	duracionFlag := flag.Duration("duracion", 0, "Optional duration to stay mapped before exiting")
	jsonFlag := flag.Bool("json", false, "Output JSON summary")
	flag.Parse()

	// The guard normalises the spelling first: ":1.0", "unix:1" or "localhost:1" are refused like ":1".
	d, err := grabador.ValidarDisplayAnidado(*displayFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-ventana-prueba: display %q is not allowed: %v\n", *displayFlag, err)
		return 2
	}

	win, err := grabador.CrearVentanaPrueba(d.Canonico(), *tituloFlag, *anchoFlag, *altoFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-ventana-prueba: error creating the window: %v\n", err)
		return 1
	}
	defer func() {
		if err := win.Cerrar(); err != nil {
			fmt.Fprintf(os.Stderr, "srf-ventana-prueba: error closing the window: %v\n", err)
			code = 1
		}
	}()

	xidHex := grabador.Hex(win.XID)
	if *jsonFlag {
		data, err := json.Marshal(map[string]any{
			"ok":      true,
			"xid":     xidHex,
			"display": d.Canonico(),
			"titulo":  *tituloFlag,
			"ancho":   *anchoFlag,
			"alto":    *altoFlag,
		})
		if err != nil {
			fmt.Fprintf(os.Stderr, "srf-ventana-prueba: error serializing json: %v\n", err)
			return 1
		}
		fmt.Println(string(data))
	} else {
		fmt.Println(xidHex)
	}

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	if *duracionFlag > 0 {
		select {
		case <-sigChan:
		case <-time.After(*duracionFlag):
		}
	} else {
		<-sigChan
	}
	return code
}
