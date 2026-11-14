package surface

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
)

// Capsule downloads: Chromium saves into /profile/Downloads (the persistent profile). This is the
// only part of a profile that leaves the capsule, and only on request: list names and sizes, or
// export one finished file to a host directory (operator ruling 2026-10-24: operator and AI).
// The helper never follows symlinks and reads regular files only; cookies, sessions and the rest
// of the profile are never listed or read.

// DownloadMax bounds one exported file (the runner buffers at most 16 MiB of output).
const DownloadMax = 15 << 20

var downloadRE = regexp.MustCompile(`^[^/\x00]{1,200}$`)

const downloadsPython = `import json, os, stat, sys
D = sys.argv.pop(1)
try:
    st = os.lstat(D)
except FileNotFoundError:
    print('[]'); sys.exit(0)
if not stat.S_ISDIR(st.st_mode):
    sys.exit(4)
if len(sys.argv) == 1:
    out = []
    for name in sorted(os.listdir(D))[:500]:
        s = os.lstat(os.path.join(D, name))
        if stat.S_ISREG(s.st_mode) and not name.endswith('.crdownload'):
            out.append({'name': name, 'bytes': s.st_size, 'mtime': int(s.st_mtime)})
    print(json.dumps(out, ensure_ascii=False)); sys.exit(0)
name = sys.argv[1]
if '/' in name or name in ('.', '..') or name.endswith('.crdownload'):
    sys.exit(2)
if sys.argv[2] == 'delete':
    path = os.path.join(D, name)
    if not stat.S_ISREG(os.lstat(path).st_mode):
        sys.exit(5)
    os.unlink(path)
    sys.exit(0)
fd = os.open(os.path.join(D, name), os.O_RDONLY | os.O_NOFOLLOW)
s = os.fstat(fd)
if not stat.S_ISREG(s.st_mode) or s.st_size > int(sys.argv[2]):
    sys.exit(5)
with os.fdopen(fd, 'rb') as f:
    sys.stdout.buffer.write(f.read(int(sys.argv[2]) + 1))
`

// Download is one finished file in the capsule's download folder.
type Download struct {
	Name  string `json:"name"`
	Bytes int64  `json:"bytes"`
	MTime int64  `json:"mtime"`
}

func (t Tool) downloadsRun(ctx context.Context, c string, args ...string) ([]byte, error) {
	s, e := t.load(c)
	if e != nil {
		return nil, e
	}
	folder := "/profile/Downloads"
	if s.Kind == "program" {
		folder = s.ProgramLaunch.ExchangeDir
	}
	a := append([]string{"docker", "exec", "-u", "10001:10001", c, "timeout", "--signal=TERM", "--kill-after=1s", "20s", "python3", "-I", "-c", downloadsPython, folder}, args...)
	return t.R.Run(ctx, a, nil)
}

// Downloads lists the finished files in the capsule's download folder.
func (t Tool) Downloads(ctx context.Context, c string) ([]Download, error) {
	if !capsuleRE.MatchString(c) {
		return nil, fail(2, "invalid identity capsule")
	}
	if t.R == nil {
		return nil, fail(3, "runner required")
	}
	raw, e := t.downloadsRun(ctx, c)
	if e != nil {
		return nil, fail(4, "capsule downloads unavailable")
	}
	var out []Download
	if json.Unmarshal(raw, &out) != nil {
		return nil, fail(5, "malformed downloads listing")
	}
	return out, nil
}

// ExportDownload copies one finished download to dir (an existing absolute host directory);
// an existing destination file is never replaced.
func (t Tool) ExportDownload(ctx context.Context, c, name, dir string) (map[string]any, error) {
	if !capsuleRE.MatchString(c) || !downloadRE.MatchString(name) || name == "." || name == ".." || !filepath.IsAbs(dir) {
		return nil, fail(2, "invalid capsule, download name or destination")
	}
	if fi, e := os.Stat(dir); e != nil || !fi.IsDir() {
		return nil, fail(4, "destination directory unavailable")
	}
	dest := filepath.Join(dir, name)
	if filepath.Dir(dest) != filepath.Clean(dir) {
		return nil, fail(2, "invalid download name")
	}
	if _, e := os.Lstat(dest); !os.IsNotExist(e) {
		return nil, fail(4, "destination file already exists")
	}
	list, e := t.Downloads(ctx, c)
	if e != nil {
		return nil, e
	}
	var want *Download
	for i := range list {
		if list[i].Name == name {
			want = &list[i]
		}
	}
	if want == nil {
		return nil, fail(4, "no such finished download")
	}
	if want.Bytes > DownloadMax {
		return nil, fail(4, "download exceeds 15 MiB export bound")
	}
	raw, e := t.downloadsRun(ctx, c, name, "15728640")
	if e != nil || int64(len(raw)) != want.Bytes {
		return nil, fail(5, "download read failed or changed")
	}
	f, e := os.OpenFile(dest, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return nil, fail(4, "destination not writable")
	}
	_, werr := f.Write(raw)
	cerr := f.Close()
	if werr != nil || cerr != nil {
		_ = os.Remove(dest)
		return nil, fail(4, "export write failed")
	}
	return map[string]any{"exported": dest, "bytes": len(raw)}, nil
}

// DeleteDownload removes one finished file from the capsule's download folder (regular files
// only; nothing else of the profile).
func (t Tool) DeleteDownload(ctx context.Context, c, name string) (map[string]any, error) {
	if !capsuleRE.MatchString(c) || !downloadRE.MatchString(name) || name == "." || name == ".." {
		return nil, fail(2, "invalid capsule or download name")
	}
	list, e := t.Downloads(ctx, c)
	if e != nil {
		return nil, e
	}
	found := false
	for _, d := range list {
		found = found || d.Name == name
	}
	if !found {
		return nil, fail(4, "no such finished download")
	}
	if _, e := t.downloadsRun(ctx, c, name, "delete"); e != nil {
		return nil, fail(5, "download delete failed")
	}
	return map[string]any{"deleted": name}, nil
}
