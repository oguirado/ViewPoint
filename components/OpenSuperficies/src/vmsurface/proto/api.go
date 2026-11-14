// Package proto is the os-vm-0 VM agent wire protocol v1 (W3 design §10.1). It is shared by the
// guest agent (cmd/srf-guest-agent), the container relay shims and srf-vm-entry. FROZEN API: lane
// AGENT implements the bodies; names, types, constants and signatures must not change.
package proto

import "io"

// Magic starts every frame. Frame = Magic | Type (1 B) | Stream (4 B BE) | Length (4 B BE) | Payload.
const Magic = "VMA1"

// MaxPayload bounds one frame payload.
const MaxPayload = 1 << 20

// Port names of the guest virtio-serial ports.
const (
	PortControl = "opensuperficies.control"
	PortAgent   = "opensuperficies.agent"
	PortView    = "opensuperficies.view"
)

type Type uint8

const (
	TOpen      Type = 1  // payload: JSON Open
	TStdin     Type = 2  // stdin chunk
	TStdinEOF  Type = 3  // no payload
	TStdout    Type = 4  // stdout chunk
	TStderr    Type = 5  // stderr chunk (client keeps the last 4 KiB)
	TExit      Type = 6  // payload: JSON Exit
	TCancel    Type = 7  // no payload
	TViewOpen  Type = 8  // no payload: new view connection
	TViewData  Type = 9  // view bytes
	TViewClose Type = 10 // no payload
)

// Frame is one decoded frame.
type Frame struct {
	Type    Type
	Stream  uint32
	Payload []byte
}

// Open starts one allow-listed command in the guest.
type Open struct {
	Argv      []string          `json:"argv"`
	Env       map[string]string `json:"env"`
	StdinCap  int64             `json:"stdin_cap"`  // -1 = stream (record helper)
	TimeoutMS int64             `json:"timeout_ms"` // 0 = stream until CANCEL or stdin EOF
}

// Exit ends a stream. RC is 0 or 2..7 for protocol refusals (5 malformed/oversize/unknown, 6 dead peer/timeout).
type Exit struct {
	RC int `json:"rc"`
}

// Error carries an exit code in 2..7 (never 1).
type Error struct {
	Code   int
	Detail string
}

func (e *Error) Error() string { return e.Detail }

func notImplemented() error { return &Error{3, "AUSENTE — vmsurface/proto not implemented"} }

// WriteFrame encodes f to w (code 5 when the payload exceeds MaxPayload).
func WriteFrame(w io.Writer, f Frame) error { return writeFrame(w, f) }

// ReadFrame decodes one frame (code 5 on bad magic, unknown type or oversize; io.EOF at a clean end).
func ReadFrame(r io.Reader) (Frame, error) { return readFrame(r) }

// Allowed validates an Open against the closed allow-list (§10.1) and returns the per-entry timeout
// to enforce; code 2 when refused. helperHashes are the sha256 hex of the allowed python3 -c sources.
func Allowed(o Open, helperHashes map[string]bool) (timeoutMS int64, err error) {
	return allowed(o, helperHashes)
}

// ForwardEnv filters an environment to the forwarded allow-list (DISPLAY=":100", NO_AT_BRIDGE in
// {"0","1"}, SRF_LOGIN in {"0","1"}); everything else is dropped.
func ForwardEnv(env []string) map[string]string { return forwardEnv(env) }
