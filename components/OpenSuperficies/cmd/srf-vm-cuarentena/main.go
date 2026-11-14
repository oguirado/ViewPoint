package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"opensuperficies.local/srf/src/displaybook/microvm"
)

func main() {
	var (
		slotFlag uint
		jsonFlag bool
	)

	flag.UintVar(&slotFlag, "slot", 3, "slot number of the microvm to evaluate")
	flag.BoolVar(&jsonFlag, "json", false, "output structured JSON evaluation")
	flag.Parse()

	meta, err := microvm.CargarMetadata(uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-vm-cuarentena: error loading metadata for slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	result, err := microvm.EvaluarPuertas(meta.Slot, meta.PID, meta.VCPUs, meta.MemMiB, meta.Rundir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-vm-cuarentena: evaluation error: %v\n", err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(result); err != nil {
			fmt.Fprintf(os.Stderr, "srf-vm-cuarentena: json encode error: %v\n", err)
			os.Exit(1)
		}
	} else {
		fmt.Printf("Evaluation for slot %d (PID %d):\n", result.Slot, result.PID)
		for _, p := range result.Puertas {
			status := "[FAIL]"
			if p.Verde {
				status = "[ OK ]"
			}
			if p.Tipo == "informativa" {
				status = "[INFO]"
			}
			fmt.Printf("  %s %s: %s\n", status, p.ID, p.Detalle)
		}
		if result.Verde {
			fmt.Println("VERDICT: ALL VERDICT GATES PASSED")
		} else {
			fmt.Println("VERDICT: ONE OR MORE VERDICT GATES FAILED")
		}
	}

	if result.Verde {
		os.Exit(0)
	}
	os.Exit(1)
}
