package quarantineoracle

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

// MountInfo representa una entrada del fichero /proc/<pid>/mountinfo de Linux.
type MountInfo struct {
	MountID        int      `json:"mount_id"`
	ParentID       int      `json:"parent_id"`
	Major          int      `json:"major"`
	Minor          int      `json:"minor"`
	Root           string   `json:"root"`
	MountPoint     string   `json:"mount_point"`
	Options        string   `json:"options"`
	OptionalFields []string `json:"optional_fields"`
	FSType         string   `json:"fs_type"`
	Source         string   `json:"source"`
	VFSOptions     string   `json:"vfs_options"`
}

// GetMountsFromReader decodifica todas las lineas de mountinfo desde un reader.
func GetMountsFromReader(r io.Reader) ([]*MountInfo, error) {
	var mounts []*MountInfo
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		mi, err := parseMountinfoLine(line)
		if err != nil {
			return nil, err
		}
		mounts = append(mounts, mi)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("error leyendo mountinfo: %w", err)
	}
	return mounts, nil
}

// ParseMountinfo es un alias de conveniencia para GetMountsFromReader.
func ParseMountinfo(r io.Reader) ([]*MountInfo, error) {
	return GetMountsFromReader(r)
}

// ParseMountinfoFile lee y decodifica un fichero mountinfo por ruta.
func ParseMountinfoFile(path string) ([]*MountInfo, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return GetMountsFromReader(f)
}

// ParsePidMountinfo lee y decodifica el mountinfo de un PID determinado.
func ParsePidMountinfo(pid int) ([]*MountInfo, error) {
	return ParseMountinfoFile(fmt.Sprintf("/proc/%d/mountinfo", pid))
}

func parseMountinfoLine(line string) (*MountInfo, error) {
	fields := strings.Fields(line)
	if len(fields) < 7 {
		return nil, fmt.Errorf("linea mountinfo demasiado corta: %q", line)
	}

	hyphenIdx := -1
	for i := 6; i < len(fields); i++ {
		if fields[i] == "-" {
			hyphenIdx = i
			break
		}
	}
	if hyphenIdx == -1 || len(fields) < hyphenIdx+3 {
		return nil, fmt.Errorf("formato mountinfo invalido, separador '-' ausente o campos truncados: %q", line)
	}

	mountID, _ := strconv.Atoi(fields[0])
	parentID, _ := strconv.Atoi(fields[1])
	devParts := strings.Split(fields[2], ":")
	major, minor := 0, 0
	if len(devParts) == 2 {
		major, _ = strconv.Atoi(devParts[0])
		minor, _ = strconv.Atoi(devParts[1])
	}

	mi := &MountInfo{
		MountID:        mountID,
		ParentID:       parentID,
		Major:          major,
		Minor:          minor,
		Root:           fields[3],
		MountPoint:     fields[4],
		Options:        fields[5],
		OptionalFields: fields[6:hyphenIdx],
		FSType:         fields[hyphenIdx+1],
		Source:         fields[hyphenIdx+2],
	}
	if len(fields) > hyphenIdx+3 {
		mi.VFSOptions = fields[hyphenIdx+3]
	}

	return mi, nil
}

// HasMountWithSubstring comprueba si algun montaje contiene una subcadena en su origen, destino o raiz.
func HasMountWithSubstring(mounts []*MountInfo, substr string) bool {
	for _, m := range mounts {
		if strings.Contains(m.Source, substr) || strings.Contains(m.MountPoint, substr) || strings.Contains(m.Root, substr) {
			return true
		}
	}
	return false
}

// FindSensitiveMountLeaks busca coincidencias con prefijos o rutas sensibles del host.
func FindSensitiveMountLeaks(mounts []*MountInfo, sensitivePaths []string) []string {
	var leaks []string
	for _, m := range mounts {
		for _, s := range sensitivePaths {
			if m.Source == s || strings.HasPrefix(m.Source, s+"/") {
				leaks = append(leaks, fmt.Sprintf("%s en %s", s, m.MountPoint))
			}
		}
	}
	return leaks
}
