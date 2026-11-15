package w8builder

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"path"
	"strings"
)

const ScanByteCap int64 = 8 << 30
const ScanWindow = 64 << 10

// ApprovedRunnerSHA256 is populated by the conductor from a reproducible,
// operator-authorized compilation of the approved recipe. Empty is unavailable,
// never a synthetic binary hash or an exemption from scanning.
const ApprovedRunnerSHA256 = "90e5ea5ee9ad6cc28265e00dc6449257ba55d70e8ffbf9f4d272e3f1783f7cbb"

var runnerVerbs = []string{"ingest", "gate", "status", "extract", "purge"}

func runnerPins() map[string]string {
	if !validDigest(ApprovedRunnerSHA256) {
		return nil
	}
	m := map[string]string{}
	for _, v := range runnerVerbs {
		m["opt/w8/"+v] = ApprovedRunnerSHA256
	}
	return m
}

// Internal seam for inert package fixtures; production always uses the compiled pins.
var defaultRunnerPins = runnerPins

type ScanPolicy struct {
	Identity   RepoIdentity
	Workspaces []string
	// RunnerSHA256 is trusted operator policy, never read from the image.
	RunnerSHA256 map[string]string
}

// ScanLayers consumes a docker-save stream without writing layers to disk.
// The cap covers headers, padding, layers and trailing bytes, not just payloads.
func ScanLayers(r io.Reader, cap int64, policies ...ScanPolicy) error {
	policy := ScanPolicy{RunnerSHA256: defaultRunnerPins()}
	if len(policies) > 1 {
		return refusal(2, "usage", "one scan policy required")
	}
	if len(policies) == 1 {
		policy = policies[0]
		if policy.RunnerSHA256 == nil {
			policy.RunnerSHA256 = defaultRunnerPins()
		}
	}
	if len(policy.RunnerSHA256) > 0 {
		if len(policy.RunnerSHA256) != len(runnerVerbs) {
			return refusal(5, "verification", "runner pin count")
		}
		for _, v := range runnerVerbs {
			if !validDigest(policy.RunnerSHA256["opt/w8/"+v]) {
				return refusal(5, "verification", "runner pin")
			}
		}
	}
	for _, root := range policy.Workspaces {
		if !strings.HasPrefix(root, "/") || path.Clean(root) != root || root == "/" {
			return refusal(2, "pin", "invalid workspace root")
		}
	}
	if cap <= 0 || cap > ScanByteCap {
		return refusal(5, "verification", "invalid scan cap")
	}
	lr := &io.LimitedReader{R: r, N: cap + 1}
	seen := map[string]bool{}
	if e := scanTar(lr, true, 0, policy, seen); e != nil {
		return e
	}
	if _, e := io.CopyBuffer(io.Discard, lr, make([]byte, ScanWindow)); e != nil {
		return refusal(5, "verification", "scan read")
	}
	if lr.N == 0 {
		return refusal(5, "verification", "layer scan byte cap exceeded")
	}
	if len(policy.RunnerSHA256) > 0 && len(seen) != len(runnerVerbs) {
		return refusal(5, "verification", "missing runner binaries")
	}
	return nil
}
func scanTar(r io.Reader, outer bool, depth int, policy ScanPolicy, seen map[string]bool) error {
	if depth > 1 {
		return refusal(5, "verification", "nested layer depth")
	}
	tr := tar.NewReader(r)
	for count := 0; count < 1000000; count++ {
		h, e := tr.Next()
		if e == io.EOF {
			return nil
		}
		if e != nil {
			return refusal(5, "verification", "invalid layer tar")
		}
		n := strings.TrimPrefix(h.Name, "./")
		if n == "." || n == "" || n == "./" {
			continue
		}
		if outer {
			if dangerousPath(strings.TrimSuffix(n, "/")) {
				return refusal(5, "verification", "unsafe layer path")
			}
		} else if unsafeImagePath(strings.TrimSuffix(n, "/")) {
			return refusal(5, "verification", "unsafe layer path")
		}
		if h.Size < 0 {
			return refusal(5, "verification", "layer size")
		}
		if outer && strings.HasPrefix(n, "blobs/sha256/") && h.Typeflag == tar.TypeReg {
			// OCI layout (docker save on the containerd image store): layers are blobs, gzip or plain tar.
			br := bufio.NewReaderSize(tr, 512)
			head, _ := br.Peek(512)
			switch {
			case len(head) >= 2 && head[0] == 0x1f && head[1] == 0x8b:
				gz, ge := gzip.NewReader(br)
				if ge != nil {
					return refusal(5, "verification", "invalid gzip layer")
				}
				if e = scanTar(gz, false, depth+1, policy, seen); e != nil {
					return e
				}
			case len(head) >= 262 && string(head[257:262]) == "ustar":
				if e = scanTar(br, false, depth+1, policy, seen); e != nil {
					return e
				}
			case len(head) >= 4 && bytes.Equal(head[:4], []byte{0x28, 0xb5, 0x2f, 0xfd}):
				return refusal(5, "verification", "unsupported zstd layer")
			default:
				// index, manifest and config JSON: metadata, not image files.
				if _, e = io.CopyBuffer(io.Discard, br, make([]byte, ScanWindow)); e != nil {
					return refusal(5, "verification", "layer read")
				}
			}
			continue
		}
		if outer && strings.HasSuffix(n, "/layer.tar") {
			if h.Typeflag != tar.TypeReg {
				return refusal(5, "verification", "layer not regular")
			}
			if e = scanTar(tr, false, depth+1, policy, seen); e != nil {
				return e
			}
			continue
		}
		// Symlinks resolve inside the container and are never followed by the scanner; any target is allowed.
		// Hard links name another archive entry and must be a clean in-image path.
		if h.Typeflag == tar.TypeLink && (strings.HasPrefix(h.Linkname, "/") || unsafeImagePath(strings.TrimPrefix(h.Linkname, "./"))) {
			return refusal(5, "verification", "unsafe image link")
		}
		if h.Typeflag == tar.TypeSymlink && strings.ContainsAny(h.Linkname, "\x00") {
			return refusal(5, "verification", "unsafe image link")
		}
		if h.Typeflag != tar.TypeReg && h.Typeflag != tar.TypeDir && h.Typeflag != tar.TypeSymlink && h.Typeflag != tar.TypeLink {
			return refusal(5, "verification", "special image entry")
		}
		lower := strings.ToLower(n)
		base := path.Base(lower)
		if base == ".git" || strings.Contains("/"+lower+"/", "/.git/") || strings.Contains("/"+lower+"/", "/.wavemind/") {
			return refusal(5, "verification", "repository metadata in image")
		}
		if strings.HasPrefix(n, "opt/w8/") && h.Typeflag == tar.TypeDir && strings.TrimSuffix(n, "/") != "opt/w8" {
			return refusal(5, "verification", "unexpected runner subdirectory")
		}
		if strings.HasPrefix(n, "opt/w8/") && h.Typeflag != tar.TypeDir {
			want, ok := policy.RunnerSHA256[n]
			if !ok || !validDigest(want) || h.Typeflag != tar.TypeReg || h.Size < 4 || h.Size > 32<<20 || seen[n] {
				return refusal(5, "verification", "unapproved runner entry")
			}
			hash := sha256.New()
			var magic [4]byte
			if _, e = io.ReadFull(tr, magic[:]); e != nil || !bytes.Equal(magic[:], []byte{0x7f, 'E', 'L', 'F'}) {
				return refusal(5, "verification", "runner not ELF")
			}
			hash.Write(magic[:])
			if _, e = io.CopyBuffer(hash, tr, make([]byte, ScanWindow)); e != nil {
				return refusal(5, "verification", "runner read")
			}
			if hex.EncodeToString(hash.Sum(nil)) != want {
				return refusal(5, "verification", "runner digest mismatch")
			}
			seen[n] = true
			continue
		}
		// OCI whiteouts (.wh.*) are deletion markers written by a later layer, not image content.
		if strings.HasPrefix(path.Base(n), ".wh.") && h.Typeflag == tar.TypeReg && h.Size == 0 {
			continue
		}
		if h.Typeflag == tar.TypeReg && !outer {
			if want, ok := baseRootDotfiles[n]; ok {
				data, re := io.ReadAll(io.LimitReader(tr, 4096))
				if re != nil {
					return refusal(5, "verification", "layer read")
				}
				if got := sha256.Sum256(data); hex.EncodeToString(got[:]) == want {
					continue
				}
				return refusal(5, "verification", "nonempty image workspace")
			}
		}
		roots := append([]string{"/src", "/workspace", "/work", "/repo", "/home", "/root", "/tmp", "/opt/w8", "/opt/cache"}, policy.Workspaces...)
		if h.Typeflag != tar.TypeDir {
			for _, root := range roots {
				if "/"+n == root || strings.HasPrefix("/"+n, root+"/") {
					return refusal(5, "verification", "nonempty image workspace")
				}
			}
		}
		if h.Typeflag == tar.TypeReg {
			hash := sha1.New()
			_, _ = fmt.Fprintf(hash, "blob %d\x00", h.Size)
			if e = scanPayload(n, io.TeeReader(tr, hash)); e != nil {
				return e
			}
			if h.Size >= 64 {
				if _, found := policy.Identity.blobs[hex.EncodeToString(hash.Sum(nil))]; found {
					return refusal(5, "verification", "target repository blob in image")
				}
			}
		}
	}
	return refusal(5, "verification", "too many layer entries")
}

// Vendor trees: upstream toolchains (sha-pinned archives) and distribution files under /usr.
// They legitimately carry icons, sample media and public test keys; captured frames never live there.
var assetRoots = []string{"usr/", "opt/toolchains/"}

func underAny(name string, roots []string) bool {
	for _, r := range roots {
		if strings.HasPrefix(name, r) {
			return true
		}
	}
	return false
}

// xwdHeader reports a plausible X Window Dump header (all fields big- or little-endian u32):
// header_size 100..65536, file_version 7, pixmap_format 0..2, pixmap_depth 1..32, width/height 1..65535.
func xwdHeader(b []byte) bool {
	if len(b) < 28 {
		return false
	}
	for _, be := range []bool{true, false} {
		u := func(o int) uint32 {
			if be {
				return binary.BigEndian.Uint32(b[o:])
			}
			return binary.LittleEndian.Uint32(b[o:])
		}
		if u(4) == 7 && u(0) >= 100 && u(0) <= 65536 && u(8) <= 2 && u(12) >= 1 && u(12) <= 32 && u(16) >= 1 && u(16) <= 65535 && u(20) >= 1 && u(20) <= 65535 {
			return true
		}
	}
	return false
}

// scanPayload enforces the STORAGE LAW and the no-secrets rule on one image file (conductor ruling, W8-B3 live build):
//   - an XWD frame header at file start is refused everywhere;
//   - an image or video container at file start (PNG, JPEG, Matroska/WebM, ISO-BMFF ftyp, YUV4MPEG2) is refused outside the vendor trees;
//   - a text file (no NUL in its first window) with a private-key PEM header at the start of a line is refused outside opt/toolchains/.
//
// Byte patterns inside binaries (libraries embedding PEM literals, executables embedding icons) are not files of those kinds.
func scanPayload(name string, r io.Reader) error {
	head := make([]byte, ScanWindow)
	n, e := io.ReadFull(r, head)
	if e != nil && e != io.EOF && e != io.ErrUnexpectedEOF {
		return refusal(5, "verification", "layer read")
	}
	head = head[:n]
	if _, e = io.CopyBuffer(io.Discard, r, make([]byte, ScanWindow)); e != nil {
		return refusal(5, "verification", "layer read")
	}
	if xwdHeader(head) {
		return refusal(5, "verification", "XWD frame")
	}
	media := bytes.HasPrefix(head, []byte{0x89, 'P', 'N', 'G', 13, 10, 26, 10}) || bytes.HasPrefix(head, []byte{0xff, 0xd8, 0xff}) ||
		bytes.HasPrefix(head, []byte{0x1a, 0x45, 0xdf, 0xa3}) || bytes.HasPrefix(head, []byte("YUV4MPEG2")) ||
		(len(head) >= 8 && bytes.Equal(head[4:8], []byte("ftyp")))
	if media && !underAny(name, assetRoots) {
		return refusal(5, "verification", "secret or frame signature")
	}
	if !bytes.Contains(head, []byte{0}) && !underAny(name, []string{"opt/toolchains/"}) {
		for _, line := range bytes.Split(head, []byte("\n")) {
			l := bytes.TrimLeft(line, " \t\r")
			if bytes.HasPrefix(l, []byte("-----BEGIN ")) && bytes.Contains(l, []byte("PRIVATE KEY-----")) {
				return refusal(5, "verification", "secret or frame signature")
			}
		}
	}
	return nil
}

// unsafeImagePath is the image-entry rule (conductor ruling, W8-B3 live build #8): traversal, backslash and NUL
// are refused, and so are host-credential names; ordinary distro names (~, $, .config, *.key) are image content,
// covered by the payload rules, not by name.
func unsafeImagePath(n string) bool {
	if n == "" || strings.HasPrefix(n, "/") || strings.ContainsAny(n, "\\\x00") {
		return true
	}
	for _, p := range strings.Split(strings.ToLower(n), "/") {
		if p == ".." || p == ".ssh" || p == ".beckett" || p == "operator.token" || p == "launch.token" || p == "auth.json" {
			return true
		}
	}
	return false
}

// baseRootDotfiles are the exact /root files of the pinned debian:bookworm-slim base layer. The scanner checks each
// layer on its own, so a later deletion (whiteout) cannot remove them from the base layer; only these exact bytes pass.
var baseRootDotfiles = map[string]string{
	"root/.bashrc":  "373b7d3b2ab90d75daf94ca16d61339d088c12020ad43b65d1b34ea80b0c0818",
	"root/.profile": "bbee58b1e0787bb851e7f7a4d0c187a8122d68eb67e5fa464696310398ac005b",
}
