package w8seat

import (
	"encoding/binary"
	"encoding/json"
	"io"
	"opensuperficies.local/srf/src/workerbridge"
)

// Every frame is a BE uint32 length followed by strict UTF-8 JSON. Only submit
// has unframed streams, in bundle then patch order, of the declared lengths.
type Request struct {
	Verb                   string          `json:"verb"`
	WorkerTask             json.RawMessage `json:"worker_task,omitempty"`
	BundleLen              int64           `json:"bundle_len,omitempty"`
	PatchLen               int64           `json:"patch_len,omitempty"`
	ObservedIntegrationTip string          `json:"observed_integration_tip,omitempty"`
	Handle                 string          `json:"handle,omitempty"`
}
type Response struct {
	ExitCode     int               `json:"exit_code"`
	Reason       string            `json:"reason,omitempty"`
	Handle       string            `json:"handle,omitempty"`
	Ready        bool              `json:"ready"`
	Receipt      json.RawMessage   `json:"receipt,omitempty"`
	Artifacts    map[string]string `json:"artifacts,omitempty"`
	BridgeResult any               `json:"bridge_result"`
}

func ReadFrame(r io.Reader, v any) error {
	var n uint32
	if e := binary.Read(r, binary.BigEndian, &n); e != nil {
		return refusal(2, "contract", "short frame")
	}
	if n == 0 || n > MaxHeader {
		return refusal(2, "contract", "header cap")
	}
	b := make([]byte, n)
	if _, e := io.ReadFull(r, b); e != nil {
		return refusal(2, "contract", "short header")
	}
	if _, ok := v.(*Request); ok {
		m, e := workerbridge.Parse(b)
		if e != nil {
			return refusal(2, "contract", "invalid frame JSON")
		}
		for _, key := range []string{"binds", "env_file", "mounts", "volumes", "devices", "privileged", "argv", "env_names"} {
			if _, present := m[key]; present {
				return refusal(5, "containment", "forbidden launch field")
			}
		}
	}
	return strictJSON(b, v)
}
func WriteFrame(w io.Writer, v any) error {
	b, e := json.Marshal(v)
	if e != nil || len(b) > MaxHeader {
		return refusal(2, "contract", "response cap")
	}
	var h [4]byte
	binary.BigEndian.PutUint32(h[:], uint32(len(b)))
	if e = writeFull(w, h[:]); e != nil {
		return e
	}
	return writeFull(w, b)
}

func writeFull(w io.Writer, b []byte) error {
	for len(b) > 0 {
		n, e := w.Write(b)
		if e != nil {
			return e
		}
		if n <= 0 || n > len(b) {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}
