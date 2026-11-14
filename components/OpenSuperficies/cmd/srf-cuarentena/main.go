package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"opensuperficies.local/srf/src/quarantineoracle"
)

func main() {
	var (
		contenedorFlag string
		capsulaFlag    string
		claseFlag      string
		socketFlag     string
		jsonFlag       bool
	)

	flag.StringVar(&contenedorFlag, "contenedor", "", "nombre o ID del contenedor a auditar")
	flag.StringVar(&capsulaFlag, "capsula", "", "alias para --contenedor")
	flag.StringVar(&claseFlag, "clase", "browser", "clase de capsula: browser o desktop")
	flag.StringVar(&socketFlag, "socket", "/var/run/docker.sock", "ruta al socket unix del daemon de Docker")
	flag.BoolVar(&jsonFlag, "json", false, "salida estructurada en JSON")

	flag.Parse()

	target := contenedorFlag
	if target == "" {
		target = capsulaFlag
	}
	if target == "" && flag.NArg() > 0 {
		target = flag.Arg(0)
	}

	if target == "" {
		fmt.Fprintf(os.Stderr, "uso: %s --contenedor <nombre|id> [--clase browser|desktop] [--json]\n", os.Args[0])
		os.Exit(2)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	dockerClient := quarantineoracle.NewDockerSocketClient(socketFlag)
	prober := quarantineoracle.NewDefaultHTTPProber(3 * time.Second)
	oracle := quarantineoracle.NewOracle(dockerClient, prober)

	resultado, _ := oracle.Evaluar(ctx, target, claseFlag)

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(resultado); err != nil {
			fmt.Fprintf(os.Stderr, "error serializando json: %v\n", err)
			os.Exit(1)
		}
		if resultado.Verde {
			os.Exit(0)
		}
		os.Exit(1)
	}

	// Formato de texto para el operador humano
	fmt.Println("14 puertas: 11 con veredicto (A, E..N) y 3 informativas (B, C, D)")
	for _, p := range resultado.Puertas {
		fmt.Printf("== %s. %s ==\n", p.Puerta, p.Nombre)
		if p.Tipo == "informativa" {
			fmt.Printf("  [INFO] %s\n", p.Detalle)
		} else if p.Verde {
			fmt.Printf("  [ OK ] %s\n", p.Detalle)
		} else {
			fmt.Printf("  [FAIL] %s\n", p.Detalle)
		}
	}

	if resultado.Verde {
		fmt.Println("VEREDICTO: TODAS LAS PUERTAS DE CUARENTENA PASARON")
		os.Exit(0)
	} else {
		fmt.Println("VEREDICTO: AL MENOS UNA PUERTA DE CUARENTENA FALLO")
		os.Exit(1)
	}
}
