package w8builder

import (
	"context"
	"fmt"
	"io"
	"regexp"
	"strings"
)

var leaseToken = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,128}$`)
var containerToken = regexp.MustCompile(`^[a-f0-9]{12}$`)

// CapsuleJournal is the B3 seat interface. CallerUID must come from SO_PEERCRED,
// or the euid on the operator-only builder path; never from task text.
// Container is the preallocated 12-hex container name passed to docker --name.
type CapsuleJournal struct {
	Kind, Lease, Image, Container string
	CallerUID                     int
}

func (j CapsuleJournal) line() (string, error) {
	if (j.Kind != "probe" && j.Kind != "task") || !leaseToken.MatchString(j.Lease) || !validDigest(strings.TrimPrefix(j.Image, "sha256:")) || !containerToken.MatchString(j.Container) || j.CallerUID < 0 {
		return "", refusal(2, "shape", "journal fields")
	}
	return fmt.Sprintf("W8_CAPSULE_CREATED kind=%s lease=%s image=%s container=%s caller_uid=%d\n", j.Kind, j.Lease, j.Image, j.Container, j.CallerUID), nil
}

// JournalCreate writes exactly one journal line before invoking the supplied
// container-create argv. A journal failure refuses creation with rc 4.
func JournalCreate(ctx context.Context, r Runner, j CapsuleJournal, argv []string, in io.Reader, out io.Writer) error {
	line, e := j.line()
	if e != nil {
		return e
	}
	if len(argv) < 3 || argv[0] != "docker" || (argv[1] != "run" && argv[1] != "create") {
		return refusal(5, "containment", "journal create argv")
	}
	expected := false
	for i := 2; i+1 < len(argv); i++ {
		if argv[i] == "--name" && argv[i+1] == j.Container {
			expected = true
		}
	}
	if !expected {
		return refusal(5, "containment", "journal container name mismatch")
	}
	if e = r.Run(ctx, []string{"systemd-cat", "-t", "w8-capsule"}, strings.NewReader(line), io.Discard); e != nil {
		return refusal(4, "unavailable", "capsule journal unavailable")
	}
	return r.Run(ctx, argv, in, out)
}
