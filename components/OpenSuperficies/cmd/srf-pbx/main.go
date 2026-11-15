package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"syscall"

	"opensuperficies.local/srf/src/surfacebook/pbx"
)

func main() {
	if len(os.Args) < 2 {
		imprimirUso()
		os.Exit(1)
	}

	mgr := pbx.NuevoPhonebookManager("")

	switch os.Args[1] {
	case "registrar":
		fs := flag.NewFlagSet("registrar", flag.ExitOnError)
		slug := fs.String("slug", "", "Extension agent slug (required)")
		rol := fs.String("rol", pbx.RoleSurface, "Extension role")
		pid := fs.Int("pid", 0, "Target process PID")
		identidad := fs.String("identidad", "", "Path to process identity JSON file")
		modelo := fs.String("modelo", "capsule", "Model designation")
		jsonOut := fs.Bool("json", false, "Emit structured JSON output")
		_ = fs.Parse(os.Args[2:])

		if *slug == "" {
			fmt.Fprintln(os.Stderr, "error: --slug is required")
			os.Exit(1)
		}

		var ident *pbx.ProcessIdentity
		targetPID := *pid

		if *identidad != "" {
			data, err := os.ReadFile(*identidad)
			if err != nil {
				fmt.Fprintf(os.Stderr, "error reading identity file %q: %v\n", *identidad, err)
				os.Exit(1)
			}
			var parsedIdent pbx.ProcessIdentity
			if err := json.Unmarshal(data, &parsedIdent); err != nil {
				fmt.Fprintf(os.Stderr, "error unmarshaling identity JSON: %v\n", err)
				os.Exit(1)
			}
			if targetPID == 0 {
				targetPID = parsedIdent.PID
			} else {
				parsedIdent.PID = targetPID
			}
			ident = &parsedIdent

			// Anti-ghost verification via provided identity specification
			if err := pbx.AntiGhostCheck(*ident); err != nil {
				fmt.Fprintf(os.Stderr, "anti-ghost check failed: %v\n", err)
				if *jsonOut {
					enc := json.NewEncoder(os.Stdout)
					_ = enc.Encode(map[string]any{
						"error":     err.Error(),
						"rechazado": true,
						"motivo":    fmt.Sprintf("anti-ghost check failed: %v", err),
					})
				}
				os.Exit(1)
			}
		} else {
			if targetPID <= 0 {
				targetPID = os.Getpid()
			}

			// 1. Verify target process exists in kernel via signal 0
			if err := syscall.Kill(targetPID, 0); err != nil {
				fmt.Fprintf(os.Stderr, "anti-ghost check failed: process %d does not exist: %v\n", targetPID, err)
				if *jsonOut {
					enc := json.NewEncoder(os.Stdout)
					_ = enc.Encode(map[string]any{
						"error":     err.Error(),
						"rechazado": true,
					})
				}
				os.Exit(1)
			}

			// 2. Read live starttime from procfs field 22
			liveStart, err := pbx.ObtenerStartTimeProcfs(targetPID)
			if err != nil {
				fmt.Fprintf(os.Stderr, "anti-ghost check failed: cannot read procfs starttime for %d: %v\n", targetPID, err)
				if *jsonOut {
					enc := json.NewEncoder(os.Stdout)
					_ = enc.Encode(map[string]any{
						"error":     err.Error(),
						"rechazado": true,
					})
				}
				os.Exit(1)
			}

			// 3. Cross-check procfs starttime independently against /proc/<pid> directory inode mtime and /proc/stat btime
			if err := pbx.CrossCheckProcfsTime(targetPID, liveStart); err != nil {
				fmt.Fprintf(os.Stderr, "anti-ghost check failed: %v\n", err)
				if *jsonOut {
					enc := json.NewEncoder(os.Stdout)
					_ = enc.Encode(map[string]any{
						"error":     err.Error(),
						"rechazado": true,
					})
				}
				os.Exit(1)
			}

			ident = &pbx.ProcessIdentity{
				PID:           targetPID,
				StartTime:     liveStart,
				StartTimeTick: liveStart,
			}
		}

		ext, err := mgr.Registrar(*slug, *rol, *modelo, targetPID, ident)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error registering extension: %v\n", err)
			os.Exit(1)
		}

		if *jsonOut {
			res := map[string]any{
				"extension":       ext.Numero,
				"numero":          ext.Numero,
				"slug":            ext.AgenteSlug,
				"agente_slug":     ext.AgenteSlug,
				"modelo":          ext.Modelo,
				"rol":             ext.Rol,
				"pid":             ext.PID,
				"start_time":      ext.StartTimeTick,
				"start_time_tick": ext.StartTimeTick,
				"registrado_en":   ext.RegistradoEn,
				"estado":          ext.Estado,
			}
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			if err := enc.Encode(res); err != nil {
				fmt.Fprintf(os.Stderr, "error encoding registration output JSON: %v\n", err)
				os.Exit(1)
			}
		} else {
			fmt.Printf("ASIGNADA EXTENSION %d a %s (%s - %s) [PID: %d]\n",
				ext.Numero, ext.AgenteSlug, ext.Modelo, ext.Rol, ext.PID)
		}

	case "phonebook":
		fs := flag.NewFlagSet("phonebook", flag.ExitOnError)
		jsonOut := fs.Bool("json", false, "Emit structured JSON output")
		_ = fs.Parse(os.Args[2:])

		lista, err := mgr.Listar()
		if err != nil {
			fmt.Fprintf(os.Stderr, "error listing phonebook: %v\n", err)
			os.Exit(1)
		}

		if *jsonOut {
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			if err := enc.Encode(lista); err != nil {
				fmt.Fprintf(os.Stderr, "error encoding phonebook output JSON: %v\n", err)
				os.Exit(1)
			}
		} else {
			fmt.Printf("OPENSWITCHBOARD // AGENDA DE EXTENSIONES IA ACTIVAS (%d ONLINE)\n", len(lista))
			fmt.Println("--------------------------------------------------------------------------------")
			fmt.Printf("%-6s | %-16s | %-24s | %-15s | %-10s\n", "EXT", "AGENTE", "MODELO", "ROL", "ESTADO")
			fmt.Println("--------------------------------------------------------------------------------")
			for _, ext := range lista {
				fmt.Printf("%-6d | %-16s | %-24s | %-15s | %-10s\n",
					ext.Numero, ext.AgenteSlug, ext.Modelo, ext.Rol, ext.Estado)
			}
		}

	case "barge", "barge-simulado":
		fs := flag.NewFlagSet("barge", flag.ExitOnError)
		jsonOut := fs.Bool("json", false, "Emit structured JSON output")
		_ = fs.Parse(os.Args[2:])

		res, err := pbx.EjecutarBargeSimulado(context.Background())
		if err != nil {
			fmt.Fprintf(os.Stderr, "error executing barge: %v\n", err)
			os.Exit(1)
		}

		if *jsonOut {
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			if err := enc.Encode(res); err != nil {
				fmt.Fprintf(os.Stderr, "error encoding barge output JSON: %v\n", err)
				os.Exit(1)
			}
		} else {
			fmt.Printf("BARGE EXITOSO: previa=%d, nueva=%d, leida_de_arrendador=%d, arriendos_revocados=%d\n",
				res.EpocaPrevia, res.EpocaNueva, res.EpocaLeidaDeArrendador, res.ArriendosRevocados)
			for _, d := range res.DetallesPasos {
				fmt.Printf("  * %s\n", d)
			}
		}

	case "baja":
		fs := flag.NewFlagSet("baja", flag.ExitOnError)
		slug := fs.String("slug", "", "Extension agent slug (required)")
		_ = fs.Parse(os.Args[2:])

		if *slug == "" {
			fmt.Fprintln(os.Stderr, "error: --slug is required")
			os.Exit(1)
		}

		if err := mgr.Baja(*slug); err != nil {
			fmt.Fprintf(os.Stderr, "error in baja: %v\n", err)
			os.Exit(1)
		}

	case "hold":
		fs := flag.NewFlagSet("hold", flag.ExitOnError)
		slug := fs.String("slug", "", "Extension agent slug (required)")
		off := fs.Bool("off", false, "Resume from hold")
		jsonOut := fs.Bool("json", false, "Emit structured JSON output")
		_ = fs.Parse(os.Args[2:])

		if *slug == "" {
			fmt.Fprintln(os.Stderr, "error: --slug is required")
			os.Exit(1)
		}

		ext, err := mgr.Hold(*slug, *off)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error in hold: %v\n", err)
			os.Exit(1)
		}

		if *jsonOut {
			enc := json.NewEncoder(os.Stdout)
			if err := enc.Encode(ext); err != nil {
				fmt.Fprintf(os.Stderr, "error encoding hold output JSON: %v\n", err)
				os.Exit(1)
			}
		} else {
			fmt.Printf("EXTENSION %d (%s) ESTADO: %s\n", ext.Numero, ext.AgenteSlug, ext.Estado)
		}

	case "snoop":
		fs := flag.NewFlagSet("snoop", flag.ExitOnError)
		slug := fs.String("slug", "", "Extension agent slug (required)")
		jsonOut := fs.Bool("json", false, "Emit structured JSON output")
		_ = fs.Parse(os.Args[2:])

		if *slug == "" {
			fmt.Fprintln(os.Stderr, "error: --slug is required")
			os.Exit(1)
		}

		ext, err := mgr.Snoop(*slug)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error in snoop: %v\n", err)
			os.Exit(1)
		}

		if *jsonOut {
			enc := json.NewEncoder(os.Stdout)
			if err := enc.Encode(map[string]any{"snoop": true, "extension": ext.Numero, "slug": ext.AgenteSlug}); err != nil {
				fmt.Fprintf(os.Stderr, "error encoding snoop output JSON: %v\n", err)
				os.Exit(1)
			}
		} else {
			fmt.Printf("SNOOP ACTIVO en extension %d (%s)\n", ext.Numero, ext.AgenteSlug)
		}

	case "whisper":
		fs := flag.NewFlagSet("whisper", flag.ExitOnError)
		slug := fs.String("slug", "", "Target extension agent slug (required)")
		msg := fs.String("msg", "", "Whisper instruction message (required)")
		_ = fs.Parse(os.Args[2:])

		if *slug == "" || *msg == "" {
			fmt.Fprintln(os.Stderr, "error: --slug and --msg are required")
			os.Exit(1)
		}

		if err := mgr.Whisper(*slug, *msg); err != nil {
			fmt.Fprintf(os.Stderr, "error in whisper: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("WHISPER ENVIADO a %s: %s\n", *slug, *msg)

	case "version":
		fmt.Println("srf-pbx v1.0.0 (CGO_ENABLED=0 linux/arm64) [OpenSuperficies NextGen]")

	default:
		fmt.Fprintf(os.Stderr, "error: unknown subcommand %q\n", os.Args[1])
		imprimirUso()
		os.Exit(1)
	}
}

func imprimirUso() {
	fmt.Println(`Uso: srf-pbx <subcomando> [opciones]

Subcomandos:
  registrar      Registra una extensión en rango 300-899 con verificación anti-ghost
  phonebook      Muestra la agenda activa de extensiones PBX
  barge          Ejecuta la escalera de 5 pasos S2 barge-in con incremento de época
  baja           Da de baja una extensión por slug
  hold           Pone o quita una extensión en espera
  snoop          Observación pasiva de una extensión
  whisper        Inyección de directiva privada a una extensión
  version        Muestra versión del binario`)
}
