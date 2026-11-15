package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"opensuperficies.local/srf/src/displaybook/puertos"
)

type DobleReservaResultado struct {
	Slot      uint16 `json:"slot"`
	PrimeroRC int    `json:"primero_rc"`
	SegundoRC int    `json:"segundo_rc"`
}

func ejecutarPar(slotVal uint, jsonOut bool) {
	plan := puertos.NuevoPlanPuertos()
	par, err := plan.Par(uint16(slotVal))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error obteniendo par para slot %d: %v\n", slotVal, err)
		os.Exit(1)
	}

	if jsonOut {
		data, err := json.Marshal(par)
		if err != nil {
			fmt.Fprintf(os.Stderr, "error serializando JSON: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(string(data))
	} else {
		fmt.Printf("slot=%d xpra=%d cdp=%d\n", par.Slot, par.Xpra, par.CDP)
	}
	os.Exit(0)
}

func ejecutarReservarDoble(slotVal uint, dirVal string) {
	primeroRC := 0
	segundoRC := 0

	l1, err1 := puertos.ReservarSlot(dirVal, uint16(slotVal))
	if err1 != nil {
		if puertos.IsLockCollision(err1) {
			primeroRC = puertos.ExitCodeLockCollision
		} else {
			primeroRC = 1
		}
	}

	l2, err2 := puertos.ReservarSlot(dirVal, uint16(slotVal))
	if err2 != nil {
		if puertos.IsLockCollision(err2) {
			segundoRC = puertos.ExitCodeLockCollision
		} else {
			segundoRC = 1
		}
	}

	if l2 != nil {
		_ = l2.Release()
	}
	if l1 != nil {
		_ = l1.Release()
	}

	res := DobleReservaResultado{
		Slot:      uint16(slotVal),
		PrimeroRC: primeroRC,
		SegundoRC: segundoRC,
	}

	data, err := json.Marshal(res)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error serializando JSON: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(string(data))
	os.Exit(0)
}

func ejecutarReservar(slotVal uint, dirVal string, holdMs uint) {
	lock, err := puertos.ReservarSlot(dirVal, uint16(slotVal))
	if err != nil {
		if puertos.IsLockCollision(err) {
			os.Exit(puertos.ExitCodeLockCollision)
		}
		fmt.Fprintf(os.Stderr, "error reservando slot %d: %v\n", slotVal, err)
		os.Exit(1)
	}
	if holdMs > 0 {
		time.Sleep(time.Duration(holdMs) * time.Millisecond)
	}
	_ = lock.Release()
	os.Exit(0)
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "uso: %s <par|reservar-doble|reservar> [opciones] o %s --slot <n> --json\n", os.Args[0], os.Args[0])
		os.Exit(2)
	}

	switch os.Args[1] {
	case "par":
		cmd := flag.NewFlagSet("par", flag.ExitOnError)
		slotVal := cmd.Uint("slot", 0, "índice de slot (0..79)")
		jsonOut := cmd.Bool("json", false, "salida en formato JSON")
		if err := cmd.Parse(os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "error parseando flags de par: %v\n", err)
			os.Exit(2)
		}
		ejecutarPar(*slotVal, *jsonOut)

	case "reservar-doble":
		cmd := flag.NewFlagSet("reservar-doble", flag.ExitOnError)
		slotVal := cmd.Uint("slot", 0, "índice de slot (0..79)")
		_ = cmd.Bool("json", false, "salida en formato JSON")
		dirVal := cmd.String("dir", "runtime/slots", "directorio de locks de slots")
		if err := cmd.Parse(os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "error parseando flags de reservar-doble: %v\n", err)
			os.Exit(2)
		}
		ejecutarReservarDoble(*slotVal, *dirVal)

	case "reservar":
		cmd := flag.NewFlagSet("reservar", flag.ExitOnError)
		slotVal := cmd.Uint("slot", 0, "índice de slot (0..79)")
		dirVal := cmd.String("dir", "runtime/slots", "directorio de locks de slots")
		holdMs := cmd.Uint("hold", 0, "milisegundos para mantener el bloqueo")
		if err := cmd.Parse(os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "error parseando flags de reservar: %v\n", err)
			os.Exit(2)
		}
		ejecutarReservar(*slotVal, *dirVal, *holdMs)

	default:
		topFlags := flag.NewFlagSet("srf-puertos", flag.ExitOnError)
		slotVal := topFlags.Uint("slot", 0, "índice de slot (0..79)")
		jsonOut := topFlags.Bool("json", false, "salida en formato JSON")
		reservarVal := topFlags.Int("reservar", -1, "reservar slot de forma exclusiva")
		dirVal := topFlags.String("dir", "runtime/slots", "directorio de locks de slots")
		holdMs := topFlags.Uint("hold", 0, "milisegundos para mantener el bloqueo")

		if err := topFlags.Parse(os.Args[1:]); err != nil {
			fmt.Fprintf(os.Stderr, "error parseando argumentos: %v\n", err)
			os.Exit(2)
		}

		if *reservarVal >= 0 {
			ejecutarReservar(uint(*reservarVal), *dirVal, *holdMs)
		} else {
			ejecutarPar(*slotVal, *jsonOut)
		}
	}
}
