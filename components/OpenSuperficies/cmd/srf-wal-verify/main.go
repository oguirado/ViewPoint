package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strconv"

	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

func main() {
	var (
		libroPath = flag.String("libro", "", "Ruta al fichero WAL en formato JSONL")
		checkClaves = flag.Bool("claves", false, "Verificar invariantes de clave D(k) <= 1 + R(k)")
		checkOrden  = flag.Bool("orden", false, "Verificar invariantes de orden LSN y temporal")
		jsonOutput  = flag.Bool("json", false, "Salida estructurada en formato JSON")
		rcMap       = flag.Bool("rc-map", false, "Mostrar mapeo completo de SrfRCMotor a SrfDesenlace")
	)

	flag.Parse()

	if *rcMap {
		mapa := wal.MapaRC()
		if *jsonOutput {
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			if err := enc.Encode(mapa); err != nil {
				fmt.Fprintf(os.Stderr, "error serializando mapa rc: %v\n", err)
				os.Exit(2)
			}
		} else {
			for rc := 0; rc < 10; rc++ {
				k := strconv.Itoa(rc)
				fmt.Printf("RC %s: %s\n", k, mapa[k])
			}
		}
		os.Exit(0)
	}

	if *libroPath == "" {
		fmt.Fprintf(os.Stderr, "error: argumento obligatorio --libro <ruta> o --rc-map ausente\n")
		os.Exit(2)
	}

	registros, err := wal.LeerRegistros(*libroPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error leyendo libro '%s': %v\n", *libroPath, err)
		os.Exit(2)
	}

	reporte := wal.VerificarLibro(registros, *checkClaves, *checkOrden)

	if *jsonOutput {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(reporte); err != nil {
			fmt.Fprintf(os.Stderr, "error serializando reporte de verificacion: %v\n", err)
			os.Exit(2)
		}
	} else {
		fmt.Printf("WAL Verifier: %s\n", *libroPath)
		fmt.Printf("  Total registros:  %d\n", reporte.TotalRegistros)
		fmt.Printf("  Claves evaluadas: %d\n", reporte.ClavesEvaluadas)
		fmt.Printf("  Violaciones:      %d\n", reporte.Violaciones)
		fmt.Printf("  Orden valido:     %t\n", reporte.OrdenValido)
		fmt.Printf("  Claves validas:   %t\n", reporte.ClavesValidas)
		if len(reporte.Detalles) > 0 {
			fmt.Println("Detalles de violaciones:")
			for _, det := range reporte.Detalles {
				fmt.Printf("  - %s\n", det)
			}
		}
	}

	if reporte.Violaciones > 0 {
		os.Exit(1)
	}
	os.Exit(0)
}
