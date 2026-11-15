package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"opensuperficies.local/srf/src/kernelbook/escalera"
	"opensuperficies.local/srf/src/kernelbook/reaper"
)

// EstresResultado contains the measured outcome of the stress test.
type EstresResultado struct {
	Sesiones         int   `json:"sesiones"`
	DobleFork        bool  `json:"doble_fork"`
	Derribar         bool  `json:"derribar"`
	Huerfanos        int   `json:"huerfanos"`
	Zombis           int   `json:"zombis"`
	Populated        int   `json:"populated"`
	CgroupsRestantes int   `json:"cgroups_restantes"`
	DuracionMs       int64 `json:"duracion_ms"`
}

func main() {
	if len(os.Args) < 2 {
		imprimirUso()
		os.Exit(1)
	}

	subcmd := os.Args[1]
	switch subcmd {
	case "estres":
		ejecutarEstres(os.Args[2:])
	case "purgar":
		ejecutarPurgar(os.Args[2:])
	default:
		fmt.Fprintf(os.Stderr, "srf-terminal: unknown subcommand %q\n", subcmd)
		imprimirUso()
		os.Exit(1)
	}
}

func imprimirUso() {
	fmt.Fprintf(os.Stderr, "Usage: srf-terminal <subcommand> [options]\n")
	fmt.Fprintf(os.Stderr, "Subcommands:\n")
	fmt.Fprintf(os.Stderr, "  estres   Run terminal session stress test\n")
	fmt.Fprintf(os.Stderr, "  purgar   Clean and teardown residual srf-term-* cgroups\n")
}

func ejecutarEstres(args []string) {
	fs := flag.NewFlagSet("estres", flag.ExitOnError)
	sesionesFlag := fs.Int("sesiones", 50, "Number of concurrent/sequential sessions")
	dobleForkFlag := fs.Bool("doble-fork", false, "Enable adversarial escape via double-fork setsid")
	derribarFlag := fs.Bool("derribar", false, "Execute 7-rung teardown ladder")
	jsonFlag := fs.Bool("json", false, "Output in JSON format")
	_ = fs.Parse(args)

	inicio := time.Now()

	rp, err := reaper.NewProcessReaper()
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-terminal: initialize subreaper: %v\n", err)
		os.Exit(1)
	}
	escalera.SetLiveReaper(rp)

	prefix := fmt.Sprintf("srf-term-%d-", os.Getpid())

	// Clean any previous runs with this prefix
	_ = escalera.Purgar(prefix, rp)

	n := *sesionesFlag
	startedCount := 0
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("%s%04d", prefix, i)
		sesion, err := escalera.LanzarSesion(escalera.SesionConfig{
			ID:        id,
			DobleFork: *dobleForkFlag,
			Reaper:    rp,
		})
		if err != nil {
			fmt.Fprintf(os.Stderr, "srf-terminal: session %d launch failed: %v\n", i, err)
			os.Exit(1)
		}
		startedCount++

		if *derribarFlag {
			if err := sesion.Derribar(); err != nil {
				fmt.Fprintf(os.Stderr, "srf-terminal: teardown session %d failed: %v\n", i, err)
			}
		} else {
			if sesion.PTY != nil {
				_ = sesion.PTY.Close()
			}
		}
	}

	// Live census of the sibling slice
	huerfanos, zombis, populated, cgroupsRestantes, err := escalera.CensoEstado(prefix)
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-terminal: census state: %v\n", err)
		os.Exit(1)
	}

	duracion := time.Since(inicio).Milliseconds()

	res := EstresResultado{
		Sesiones:         startedCount,
		DobleFork:        *dobleForkFlag,
		Derribar:         *derribarFlag,
		Huerfanos:        huerfanos,
		Zombis:           zombis,
		Populated:        populated,
		CgroupsRestantes: cgroupsRestantes,
		DuracionMs:       duracion,
	}

	if *jsonFlag {
		data, err := json.Marshal(res)
		if err != nil {
			fmt.Fprintf(os.Stderr, "srf-terminal: marshal json: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(string(data))
	} else {
		fmt.Printf("sessions=%d double_fork=%v teardown=%v orphans=%d zombies=%d populated=%d remaining_cgroups=%d duration_ms=%d\n",
			res.Sesiones, res.DobleFork, res.Derribar, res.Huerfanos, res.Zombis, res.Populated, res.CgroupsRestantes, res.DuracionMs)
	}
}

func ejecutarPurgar(args []string) {
	fs := flag.NewFlagSet("purgar", flag.ExitOnError)
	jsonFlag := fs.Bool("json", false, "Output in JSON format")
	_ = fs.Parse(args)

	rp, err := reaper.NewProcessReaper()
	if err == nil {
		escalera.SetLiveReaper(rp)
	}

	err = escalera.Purgar("srf-term-", rp)
	if err != nil {
		if *jsonFlag {
			fmt.Printf("{\"ok\":false,\"error\":%q}\n", err.Error())
		} else {
			fmt.Fprintf(os.Stderr, "srf-terminal: purge failed: %v\n", err)
		}
		os.Exit(1)
	}

	if *jsonFlag {
		fmt.Println("{\"ok\":true,\"purged\":true}")
	}
}
