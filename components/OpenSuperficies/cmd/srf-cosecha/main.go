package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"opensuperficies.local/srf/src/databook/cas"
)

type ReporteCosecha struct {
	Entrada           string `json:"entrada"`
	Salida            string `json:"salida"`
	Caracteres        int    `json:"caracteres"`
	FuentesTotal      int    `json:"fuentes_total"`
	FuentesUnicas     int    `json:"fuentes_unicas"`
	FuentesOriginales int    `json:"fuentes_originales"`
	CitasEliminadas   int    `json:"citas_eliminadas"`
	SHA256            string `json:"sha256"`
	CASPath           string `json:"cas_path"`
	CASVerificado     bool   `json:"cas_verificado"`
}

func main() {
	entradaFlag := flag.String("entrada", "", "path to raw harvest file (HTML or Markdown)")
	salidaFlag := flag.String("salida", "", "destination path for clean harvest (Markdown)")
	casDirFlag := flag.String("cas-dir", "_cas/cosechas", "root directory of the CAS store")
	jsonFlag := flag.Bool("json", false, "emit report in JSON format to stdout")

	flag.Parse()

	if *entradaFlag == "" || *salidaFlag == "" {
		fmt.Fprintf(os.Stderr, "error: --entrada and --salida are required\n")
		flag.Usage()
		os.Exit(2)
	}

	crudo, err := os.ReadFile(*entradaFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error reading input %s: %v\n", *entradaFlag, err)
		os.Exit(1)
	}

	res, err := cas.LimpiarCosecha(string(crudo))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error cleaning harvest: %v\n", err)
		os.Exit(1)
	}

	if err := os.MkdirAll(filepath.Dir(*salidaFlag), 0755); err != nil {
		fmt.Fprintf(os.Stderr, "error creating destination directory %s: %v\n", filepath.Dir(*salidaFlag), err)
		os.Exit(1)
	}

	if err := os.WriteFile(*salidaFlag, []byte(res.Markdown), 0644); err != nil {
		fmt.Fprintf(os.Stderr, "error writing output %s: %v\n", *salidaFlag, err)
		os.Exit(1)
	}

	almacen, err := cas.NuevoAlmacenCAS(*casDirFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error initializing CAS store in %s: %v\n", *casDirFlag, err)
		os.Exit(1)
	}

	hashHex, casPath, err := almacen.Almacenar([]byte(res.Markdown))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error storing in CAS: %v\n", err)
		os.Exit(1)
	}

	casVerificado, verifyErr := almacen.Verificar(hashHex)
	if verifyErr != nil {
		casVerificado = false
	}

	reporte := ReporteCosecha{
		Entrada:           *entradaFlag,
		Salida:            *salidaFlag,
		Caracteres:        res.Caracteres,
		FuentesTotal:      res.FuentesTotal,
		FuentesUnicas:     res.FuentesUnicas,
		FuentesOriginales: res.FuentesOriginales,
		CitasEliminadas:   res.CitasEliminadas,
		SHA256:            hashHex,
		CASPath:           casPath,
		CASVerificado:     casVerificado,
	}

	if *jsonFlag {
		data, err := json.MarshalIndent(reporte, "", "  ")
		if err != nil {
			fmt.Fprintf(os.Stderr, "error serializing JSON: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(string(data))
	} else {
		fmt.Printf("Clean harvest: %d characters, %d sources (of %d original), %d cites purged\nCAS SHA256: %s (%s)\n",
			reporte.Caracteres, reporte.FuentesTotal, reporte.FuentesOriginales, reporte.CitasEliminadas, reporte.SHA256, reporte.CASPath)
	}

	if verifyErr != nil || !casVerificado {
		fmt.Fprintf(os.Stderr, "error in cold CAS verification of %s: %v\n", hashHex, verifyErr)
		os.Exit(1)
	}

	os.Exit(0)
}
