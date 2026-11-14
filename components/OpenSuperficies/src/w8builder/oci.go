package w8builder

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"strings"
)

type ociResult struct{ Manifest, Config string }
type ociDescriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
	Platform  struct {
		Architecture string `json:"architecture"`
		OS           string `json:"os"`
	} `json:"platform"`
}
type ociIndex struct {
	SchemaVersion int             `json:"schemaVersion"`
	Manifests     []ociDescriptor `json:"manifests"`
}
type ociManifest struct {
	SchemaVersion int             `json:"schemaVersion"`
	Config        ociDescriptor   `json:"config"`
	Layers        []ociDescriptor `json:"layers"`
}

func hashOCI(r io.Reader) (ociResult, error) {
	var result ociResult
	lr := &io.LimitedReader{R: r, N: ScanByteCap + 1}
	tr := tar.NewReader(lr)
	metadata := map[string][]byte{}
	sizes := map[string]int64{}
	var index []byte
	budget := 0
	seen := map[string]bool{}
	for count := 0; count < 100000; count++ {
		h, e := tr.Next()
		if e == io.EOF {
			break
		}
		if e != nil {
			return result, refusal(5, "verification", "invalid OCI archive")
		}
		n := strings.TrimPrefix(h.Name, "./")
		if h.Typeflag == tar.TypeDir {
			continue
		}
		if h.Typeflag != tar.TypeReg || dangerousPath(n) || seen[n] {
			return result, refusal(5, "verification", "OCI entry shape")
		}
		seen[n] = true
		if n == "index.json" {
			index, e = boundedRead(tr, 1<<20)
			if e != nil {
				return result, refusal(5, "verification", "OCI index cap")
			}
			continue
		}
		if strings.HasPrefix(n, "blobs/sha256/") {
			want := strings.TrimPrefix(n, "blobs/sha256/")
			if !validDigest(want) {
				return result, refusal(5, "verification", "OCI blob name")
			}
			hash := sha256.New()
			var meta bytes.Buffer
			var writer io.Writer = hash
			if h.Size <= 1<<20 {
				writer = io.MultiWriter(hash, &meta)
			}
			if _, e = io.CopyBuffer(writer, tr, make([]byte, ScanWindow)); e != nil {
				return result, refusal(5, "verification", "OCI blob read")
			}
			if hex.EncodeToString(hash.Sum(nil)) != want {
				return result, refusal(5, "verification", "OCI blob digest mismatch")
			}
			sizes["sha256:"+want] = h.Size
			if meta.Len() > 0 && bytes.HasPrefix(bytes.TrimSpace(meta.Bytes()), []byte("{")) {
				budget += meta.Len()
				if budget > 8<<20 {
					return result, refusal(5, "verification", "OCI metadata cap")
				}
				metadata["sha256:"+want] = meta.Bytes()
			}
			continue
		}
		if n != "oci-layout" {
			return result, refusal(5, "verification", "unknown OCI entry")
		}
	}
	if _, e := io.CopyBuffer(io.Discard, lr, make([]byte, ScanWindow)); e != nil || lr.N == 0 {
		return result, refusal(5, "verification", "OCI stream cap/read")
	}
	var idx ociIndex
	if e := json.Unmarshal(index, &idx); e != nil || idx.SchemaVersion != 2 || len(idx.Manifests) != 1 {
		return result, refusal(5, "verification", "OCI index must select one image")
	}
	d := idx.Manifests[0]
	if !imageID(d.Digest) || d.Size != sizes[d.Digest] {
		return result, refusal(5, "verification", "OCI manifest descriptor")
	}
	// Some exporters omit platform on the single descriptor; the config below
	// must still prove linux/arm64. An explicitly different platform is refused.
	if d.Platform.Architecture != "" && d.Platform.Architecture != "arm64" {
		return result, refusal(5, "verification", "OCI platform")
	}
	var manifest ociManifest
	if e := json.Unmarshal(metadata[d.Digest], &manifest); e != nil || manifest.SchemaVersion != 2 {
		return result, refusal(5, "verification", "OCI manifest")
	}
	if !imageID(manifest.Config.Digest) || manifest.Config.Size != sizes[manifest.Config.Digest] {
		return result, refusal(5, "verification", "OCI config descriptor")
	}
	for _, layer := range manifest.Layers {
		if !imageID(layer.Digest) || layer.Size != sizes[layer.Digest] {
			return result, refusal(5, "verification", "OCI layer descriptor")
		}
	}
	var config struct {
		Architecture string `json:"architecture"`
		OS           string `json:"os"`
		Config       struct {
			User       string   `json:"User"`
			Entrypoint []string `json:"Entrypoint"`
		} `json:"config"`
	}
	if e := json.Unmarshal(metadata[manifest.Config.Digest], &config); e != nil || config.Architecture != "arm64" || config.OS != "linux" || config.Config.User != "10001:10001" || strings.Join(config.Config.Entrypoint, "\x00") != "/usr/bin/tini\x00-s\x00--" {
		return result, refusal(5, "verification", "OCI runtime user/entrypoint/platform")
	}
	result.Manifest = strings.TrimPrefix(d.Digest, "sha256:")
	result.Config = strings.TrimPrefix(manifest.Config.Digest, "sha256:")
	return result, nil
}
