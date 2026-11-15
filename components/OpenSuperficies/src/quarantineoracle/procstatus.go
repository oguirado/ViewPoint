package quarantineoracle

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

// ProcStatus almacena los campos decodificados de /proc/<pid>/status.
type ProcStatus struct {
	Name           string            `json:"name"`
	State          string            `json:"state"`
	Pid            int               `json:"pid"`
	PPid           int               `json:"ppid"`
	TracerPid      int               `json:"tracer_pid"`
	Uid            [4]int            `json:"uid"` // real, efectivo, guardado, fs
	Gid            [4]int            `json:"gid"` // real, efectivo, guardado, fs
	FDSize         int               `json:"fd_size"`
	Groups         []int             `json:"groups"`
	CapInh         uint64            `json:"cap_inh"`
	CapPrm         uint64            `json:"cap_prm"`
	CapEff         uint64            `json:"cap_eff"`
	CapBnd         uint64            `json:"cap_bnd"`
	CapAmb         uint64            `json:"cap_amb"`
	NoNewPrivs     int               `json:"no_new_privs"`
	Seccomp        int               `json:"seccomp"`
	SeccompFilters int               `json:"seccomp_filters"`
	Threads        int               `json:"threads"`
	Raw            map[string]string `json:"raw"`
}

// ParseStatus decodifica un stream de /proc/<pid>/status en una sola pasada.
func ParseStatus(r io.Reader) (*ProcStatus, error) {
	ps := &ProcStatus{
		Raw: make(map[string]string),
	}

	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := scanner.Text()
		idx := strings.IndexByte(line, ':')
		if idx < 0 {
			continue
		}
		key := strings.TrimSpace(line[:idx])
		val := strings.TrimSpace(line[idx+1:])
		ps.Raw[key] = val

		switch key {
		case "Name":
			ps.Name = val
		case "State":
			ps.State = val
		case "Pid":
			ps.Pid, _ = strconv.Atoi(val)
		case "PPid":
			ps.PPid, _ = strconv.Atoi(val)
		case "TracerPid":
			ps.TracerPid, _ = strconv.Atoi(val)
		case "Uid":
			parseIDQuad(val, &ps.Uid)
		case "Gid":
			parseIDQuad(val, &ps.Gid)
		case "FDSize":
			ps.FDSize, _ = strconv.Atoi(val)
		case "Groups":
			for _, gStr := range strings.Fields(val) {
				if g, err := strconv.Atoi(gStr); err == nil {
					ps.Groups = append(ps.Groups, g)
				}
			}
		case "CapInh":
			ps.CapInh, _ = strconv.ParseUint(val, 16, 64)
		case "CapPrm":
			ps.CapPrm, _ = strconv.ParseUint(val, 16, 64)
		case "CapEff":
			ps.CapEff, _ = strconv.ParseUint(val, 16, 64)
		case "CapBnd":
			ps.CapBnd, _ = strconv.ParseUint(val, 16, 64)
		case "CapAmb":
			ps.CapAmb, _ = strconv.ParseUint(val, 16, 64)
		case "NoNewPrivs":
			ps.NoNewPrivs, _ = strconv.Atoi(val)
		case "Seccomp":
			ps.Seccomp, _ = strconv.Atoi(val)
		case "Seccomp_filters":
			ps.SeccompFilters, _ = strconv.Atoi(val)
		case "Threads":
			ps.Threads, _ = strconv.Atoi(val)
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("error leyendo proc status: %w", err)
	}

	return ps, nil
}

// ParseStatusFile lee y decodifica /proc/<pid>/status desde una ruta de fichero.
func ParseStatusFile(path string) (*ProcStatus, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return ParseStatus(f)
}

// ParsePidStatus lee y decodifica /proc/<pid>/status dado un PID del sistema.
func ParsePidStatus(pid int) (*ProcStatus, error) {
	return ParseStatusFile(fmt.Sprintf("/proc/%d/status", pid))
}

func parseIDQuad(val string, target *[4]int) {
	fields := strings.Fields(val)
	for i := 0; i < len(fields) && i < 4; i++ {
		if n, err := strconv.Atoi(fields[i]); err == nil {
			target[i] = n
		}
	}
}
