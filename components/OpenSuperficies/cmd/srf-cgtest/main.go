package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/kernelbook/cgroup"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

type CgTestResult struct {
	NacidoEnHermano    bool   `json:"nacido_en_hermano"`
	NacidoDentro       bool   `json:"nacido_dentro"`
	PopulatedFinal     int    `json:"populated_final"`
	CgroupEliminado    bool   `json:"cgroup_eliminado"`
	ModoNacimiento     string `json:"modo_nacimiento"`
	ErrorPrimerIntento string `json:"error_primer_intento,omitempty"`
	ErrorObservado     string `json:"error_observado,omitempty"`
}

func main() {
	jsonFlag := flag.Bool("json", false, "Output results in JSON format")
	cloneFlag := flag.Bool("clone-into-cgroup", true, "Attempt atomic CLONE_INTO_CGROUP birth")
	flag.Parse()

	result := runTest(*cloneFlag)

	if *jsonFlag {
		data, err := json.Marshal(result)
		if err != nil {
			fmt.Fprintf(os.Stderr, "json marshal error: %v\n", err)
			os.Exit(1)
		}
		fmt.Println(string(data))
	} else {
		fmt.Printf("modo_nacimiento=%s nacido_en_hermano=%v nacido_dentro=%v populated_final=%d cgroup_eliminado=%v\n",
			result.ModoNacimiento, result.NacidoEnHermano, result.NacidoDentro, result.PopulatedFinal, result.CgroupEliminado)
	}
}

func isPidInCurrentCage(pid int) bool {
	curr, err := cgroup.FindCurrentCgroup()
	if err != nil {
		return false
	}
	f, err := os.Open(filepath.Join(curr, "cgroup.procs"))
	if err != nil {
		return false
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	target := strconv.Itoa(pid)
	for scanner.Scan() {
		if strings.TrimSpace(scanner.Text()) == target {
			return true
		}
	}
	return false
}

func runTest(attemptClone bool) CgTestResult {
	result := CgTestResult{}

	// 1. Create sibling cgroup under parent slice (ia.slice/srf-cgtest)
	cg, err := cgroup.CreateSibling("srf-cgtest")
	if err != nil {
		fmt.Fprintf(os.Stderr, "CreateSibling failed: %v\n", err)
		return result
	}
	defer func() {
		_ = cg.Destroy()
	}()

	// 2. Launch child mediated by lanzador (bash sleep 30)
	cmd, err := lanzador.Command("bash", "-c", "sleep 30")
	if err != nil {
		fmt.Fprintf(os.Stderr, "lanzador.Command failed: %v\n", err)
		return result
	}

	if attemptClone {
		// First attempt: atomic birth via CLONE_INTO_CGROUP (SysProcAttr.UseCgroupFD / CgroupFD)
		cmd.SysProcAttr = &syscall.SysProcAttr{
			UseCgroupFD: true,
			CgroupFD:    cg.Fd(),
			Setpgid:     true,
		}

		if err = cmd.Start(); err == nil {
			result.ModoNacimiento = "clone_into_cgroup"
		} else {
			result.ErrorPrimerIntento = err.Error()
			result.ErrorObservado = err.Error()
		}
	}

	if result.ModoNacimiento == "" {
		result.ModoNacimiento = "migracion"
		cmd, err = lanzador.Command("bash", "-c", "sleep 30")
		if err != nil {
			fmt.Fprintf(os.Stderr, "fallback lanzador.Command failed: %v\n", err)
			return result
		}
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		if err := cmd.Start(); err != nil {
			fmt.Fprintf(os.Stderr, "cmd.Start fallback failed: %v\n", err)
			return result
		}
		if err := cg.AddProc(cmd.Process.Pid); err != nil {
			fmt.Fprintf(os.Stderr, "cg.AddProc failed: %v\n", err)
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
			return result
		}
	}

	// 3. Confirm process is in sibling cgroup
	pids, err := cg.Procs()
	if err == nil {
		for _, p := range pids {
			if p == cmd.Process.Pid {
				result.NacidoEnHermano = true
				break
			}
		}
	}

	// Define nacido_dentro as 'observed inside the command cage leaf' (must be false)
	result.NacidoDentro = isPidInCurrentCage(cmd.Process.Pid)

	// 4. Teardown ladder: kill cgroup, wait for populated 0, reap, destroy cgroup
	if err := cg.Kill(); err != nil {
		fmt.Fprintf(os.Stderr, "cg.Kill failed: %v\n", err)
	}

	_ = cg.WaitEmpty(2 * time.Second)

	popFinal, err := cg.Populated()
	if err == nil {
		result.PopulatedFinal = popFinal
	}

	// Clean up child process
	if cmd != nil && cmd.Process != nil {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}

	// 5. Remove cgroup and verify deletion
	destroyErr := cg.Destroy()
	if destroyErr == nil {
		if _, statErr := os.Stat(cg.Path); os.IsNotExist(statErr) {
			result.CgroupEliminado = true
		}
	}

	return result
}
