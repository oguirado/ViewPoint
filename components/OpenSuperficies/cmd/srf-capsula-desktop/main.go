package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"opensuperficies.local/srf/src/displaybook/desktop"
)

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(2)
	}

	subcommand := os.Args[1]
	args := os.Args[2:]

	switch subcommand {
	case "construir":
		runConstruir(args)
	case "iniciar":
		runIniciar(args)
	case "detener":
		runDetener(args)
	case "pid":
		runPID(args)
	case "listar":
		runListar(args)
	case "percepcion":
		runPercepcion(args)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", subcommand)
		printUsage()
		os.Exit(2)
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, "usage: %s <construir|iniciar|detener|pid|listar|percepcion> [options]\n", os.Args[0])
}

func runConstruir(args []string) {
	fs := flag.NewFlagSet("construir", flag.ExitOnError)
	var (
		claseFlag string
		tagFlag   string
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "desktop", "capsule class")
	fs.StringVar(&tagFlag, "tag", desktop.DefaultImageTag, "OCI image tag")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	opts := desktop.BuildOptions{
		Tag:   tagFlag,
		Clase: claseFlag,
	}

	res, err := desktop.Construir(context.Background(), opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error building desktop image: %v\n", err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(res); err != nil {
			fmt.Fprintf(os.Stderr, "error encoding json: %v\n", err)
			os.Exit(1)
		}
	} else {
		fmt.Println(res.Imagen)
	}
}

func runIniciar(args []string) {
	fs := flag.NewFlagSet("iniciar", flag.ExitOnError)
	var (
		claseFlag string
		slotFlag  uint
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "desktop", "capsule class")
	fs.UintVar(&slotFlag, "slot", 0, "assigned slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	opts := desktop.IniciarOptions{
		Clase: claseFlag,
	}
	info, err := desktop.Iniciar(context.Background(), uint16(slotFlag), opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error starting desktop capsule slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		// U08-F8: Redact bearer token if any is ever present
		infoOut := *info
		if infoOut.Token != "" {
			infoOut.Token = "[REDACTED]"
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(infoOut); err != nil {
			fmt.Fprintf(os.Stderr, "error encoding json: %v\n", err)
			os.Exit(1)
		}
	} else {
		fmt.Printf("desktop capsule %s started on slot %d (xpra: %d)\n", info.Contenedor, info.Slot, info.PuertoXpra)
	}
}

func runDetener(args []string) {
	fs := flag.NewFlagSet("detener", flag.ExitOnError)
	var (
		claseFlag string
		slotFlag  uint
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "desktop", "capsule class")
	fs.UintVar(&slotFlag, "slot", 0, "assigned slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	err := desktop.Detener(context.Background(), uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error stopping desktop slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot":   slotFlag,
			"clase":  claseFlag,
			"estado": "detenido",
			"rc":     0,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			fmt.Fprintf(os.Stderr, "error encoding json: %v\n", err)
			os.Exit(1)
		}
	} else {
		fmt.Printf("desktop slot %d stopped\n", slotFlag)
	}
}

func runPID(args []string) {
	fs := flag.NewFlagSet("pid", flag.ExitOnError)
	var (
		slotFlag    uint
		procesoFlag string
		jsonFlag    bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "slot number")
	fs.StringVar(&procesoFlag, "proceso", "openbox", "process name to query")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	pid, err := desktop.PID(context.Background(), uint16(slotFlag), procesoFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error resolving pid slot %d (%s): %v\n", slotFlag, procesoFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot":    slotFlag,
			"proceso": procesoFlag,
			"pid":     pid,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			fmt.Fprintf(os.Stderr, "error encoding json: %v\n", err)
			os.Exit(1)
		}
	} else {
		fmt.Println(pid)
	}
}

func runListar(args []string) {
	fs := flag.NewFlagSet("listar", flag.ExitOnError)
	var jsonFlag bool
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	list, err := desktop.Listar(context.Background())
	if err != nil {
		fmt.Fprintf(os.Stderr, "error listing desktop capsules: %v\n", err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(list); err != nil {
			fmt.Fprintf(os.Stderr, "error encoding json: %v\n", err)
			os.Exit(1)
		}
	} else {
		for _, c := range list {
			fmt.Printf("slot %d: %s (%s) xpra=%d\n", c.Slot, c.Contenedor, c.Clase, c.PuertoXpra)
		}
	}
}

func runPercepcion(args []string) {
	fs := flag.NewFlagSet("percepcion", flag.ExitOnError)
	var (
		slotFlag   uint
		dispFlag   string
		insideFlag bool
		jsonFlag   bool
	)
	fs.UintVar(&slotFlag, "slot", 2, "slot number")
	fs.StringVar(&dispFlag, "display", ":100", "X display")
	fs.BoolVar(&insideFlag, "inside", false, "run directly against local display and dbus")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	var res *desktop.PercepcionResult
	var err error
	if insideFlag {
		res, err = desktop.ProbarPercepcionLocal(dispFlag)
	} else {
		res, err = desktop.ProbarPercepcion(context.Background(), uint16(slotFlag))
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error verifying perception ladder: %v\n", err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(res); err != nil {
			fmt.Fprintf(os.Stderr, "error encoding json: %v\n", err)
			os.Exit(1)
		}
	} else {
		fmt.Printf("Perception ladder verified: root=%s (%s), xtest motion=(%d, %d)\n",
			res.NodoRaiz, res.RolNodoRaiz, res.InyeccionXTest.PunteroObservado[0], res.InyeccionXTest.PunteroObservado[1])
	}
}
