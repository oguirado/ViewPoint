package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: %s <simular-reanudacion> [flags]\n", os.Args[0])
		os.Exit(1)
	}

	subcmd := os.Args[1]
	switch subcmd {
	case "simular-reanudacion":
		fs := flag.NewFlagSet("simular-reanudacion", flag.ExitOnError)
		capsula := fs.String("capsula", "browser-slot-0", "capsule identifier")
		destino := fs.Uint("destino", 9220, "destination numeric identifier")
		jsonFlag := fs.Bool("json", false, "output in JSON format")

		_ = fs.Parse(os.Args[2:])

		res := arrendador.SimularReanudacion(*capsula, uint32(*destino))
		if *jsonFlag {
			b, err := json.Marshal(res)
			if err != nil {
				fmt.Fprintf(os.Stderr, "error serializing json: %v\n", err)
				os.Exit(1)
			}
			fmt.Println(string(b))
		} else {
			fmt.Printf("Resume simulation: zombie_rejected=%v constant_time=%v epochA=%d epochB=%d\n",
				res.EscrituraZombiRechazada, res.ComparacionTiempoConstante, res.EpocaTitularA, res.EpocaTitularB)
		}
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", subcmd)
		os.Exit(1)
	}
}
