package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "uso: %s <simular|estado> [flags]\n", os.Args[0])
		os.Exit(1)
	}

	subcmd := os.Args[1]
	switch subcmd {
	case "simular":
		fs := flag.NewFlagSet("simular", flag.ExitOnError)
		n := fs.Int("n", 24, "numero de solicitudes a simular")
		clase := fs.String("clase", "browser", "clase de capsula (browser, desktop, microvm, terminal)")
		paginas := fs.Int("paginas", 6, "numero de paginas (para browser)")
		memDispMiB := fs.Uint64("mem-disponible-mib", 80400, "memoria disponible en MiB a simular")
		jsonFlag := fs.Bool("json", false, "salida en formato JSON")

		_ = fs.Parse(os.Args[2:])

		res := gobernador.Simular(*n, *clase, *paginas, *memDispMiB)
		if *jsonFlag {
			b, err := json.Marshal(res)
			if err != nil {
				fmt.Fprintf(os.Stderr, "error serializando json: %v\n", err)
				os.Exit(1)
			}
			fmt.Println(string(b))
		} else {
			fmt.Printf("Simulacion: solicitadas=%d admitidas=%d denegadas=%d desbordamiento=%v\n",
				res.TotalSolicitadas, res.Admitidas, res.Denegadas, res.Desbordamiento)
		}

	case "estado":
		fs := flag.NewFlagSet("estado", flag.ExitOnError)
		jsonFlag := fs.Bool("json", false, "salida en formato JSON")

		_ = fs.Parse(os.Args[2:])

		tel := gobernador.NuevaTelemetriaProc(nil)
		gov := gobernador.NuevoGobernador(tel)
		est, err := gov.Estado()
		if err != nil && est.MemDisponibleBytes == 0 {
			fmt.Fprintf(os.Stderr, "error leyendo telemetria: %v\n", err)
			os.Exit(1)
		}

		if *jsonFlag {
			b, err := json.Marshal(est)
			if err != nil {
				fmt.Fprintf(os.Stderr, "error serializando json: %v\n", err)
				os.Exit(1)
			}
			fmt.Println(string(b))
		} else {
			fmt.Printf("Estado: origen=%s mem_disponible=%d bytes microvm_max=%d\n",
				est.OrigenMem, est.MemDisponibleBytes, est.MicrovmActivasMax)
		}

	default:
		fmt.Fprintf(os.Stderr, "subcomando desconocido: %s\n", subcmd)
		os.Exit(1)
	}
}
