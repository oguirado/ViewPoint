package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"opensuperficies.local/srf/src/dockerapi"
)

func printUsage() {
	fmt.Fprintf(os.Stderr, "Uso: srf-docker <subcomando> [argumentos...]\n\n"+
		"Subcomandos:\n"+
		"  inspeccionar-imagen <nombre> [--json]  Inspecciona una imagen local de Docker\n"+
		"  ping                                  Comprueba conectividad con el daemon Docker\n"+
		"  version                               Muestra versión del daemon Docker\n")
}

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(1)
	}

	cmd := os.Args[1]
	args := os.Args[2:]

	socketPath := os.Getenv("DOCKER_HOST")
	if strings.HasPrefix(socketPath, "unix://") {
		socketPath = strings.TrimPrefix(socketPath, "unix://")
	} else if socketPath == "" {
		socketPath = dockerapi.DefaultSocketPath
	}

	client, err := dockerapi.NewClient(socketPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error inicializando cliente docker: %v\n", err)
		os.Exit(1)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	switch cmd {
	case "inspeccionar-imagen":
		var imageName string
		jsonOutput := false

		for _, arg := range args {
			if arg == "--json" {
				jsonOutput = true
			} else if !strings.HasPrefix(arg, "-") && imageName == "" {
				imageName = arg
			}
		}

		if imageName == "" {
			fmt.Fprintf(os.Stderr, "error: falta el nombre de la imagen\n")
			os.Exit(1)
		}

		raw, err := client.InspectImageRaw(ctx, imageName)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error inspeccionando imagen %s: %v\n", imageName, err)
			os.Exit(1)
		}

		if jsonOutput {
			fmt.Println(string(raw))
			return
		}

		var inspect dockerapi.ImageInspect
		if err := json.Unmarshal(raw, &inspect); err != nil {
			fmt.Println(string(raw))
			return
		}
		fmt.Printf("ID: %s\nSize: %d bytes\nTags: %v\n", inspect.Id, inspect.Size, inspect.RepoTags)

	case "ping":
		if err := client.Ping(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "ping fallido: %v\n", err)
			os.Exit(1)
		}
		fmt.Println("OK")

	case "version":
		raw, err := client.InspectRaw(ctx, "/version")
		if err != nil {
			fmt.Fprintf(os.Stderr, "version fallida: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(string(raw))

	case "help", "--help", "-h", "ayuda":
		printUsage()
		os.Exit(0)

	default:
		fmt.Fprintf(os.Stderr, "subcomando desconocido: %s\n", cmd)
		printUsage()
		os.Exit(1)
	}
}
