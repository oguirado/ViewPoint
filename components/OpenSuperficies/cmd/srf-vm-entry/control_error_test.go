package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"testing"
	"time"
)

func TestGuestErrorRefusesWithoutPeerEOF(t *testing.T) {
	for _, prefix := range []string{
		"",
		"SRF1 OUT xpra layer up\n",
		"SRF1 OUT PROGRAM_READY {\"windows\":[]}\n",
		"SRF1 OUT xpra layer up\nSRF1 OUT PROGRAM_READY {\"windows\":[]}\n",
	} {
		t.Run(prefix, func(t *testing.T) {
			local, guest := net.Pipe()
			defer local.Close()
			defer guest.Close()
			deadline := time.Now().Add(2 * time.Second)
			_ = local.SetDeadline(deadline)
			_ = guest.SetDeadline(deadline)
			var out bytes.Buffer
			done := make(chan error, 1)
			go func() { done <- relayControl(local, &out) }()
			// Leave the peer open: ERR must end supervision without waiting for
			// guest poweroff, EOF or the outer readiness timeout.
			if _, err := io.WriteString(guest, prefix+"SRF1 ERR program stack exited\n"); err != nil {
				t.Fatal(err)
			}
			select {
			case err := <-done:
				var refusal *proto.Error
				if !errors.As(err, &refusal) || refusal.Code != 4 {
					t.Fatalf("guest error status: %v", err)
				}
				want := "REFUSED: guest program stack exited\n"
				if prefix == "SRF1 OUT xpra layer up\nSRF1 OUT PROGRAM_READY {\"windows\":[]}\n" {
					want = "xpra layer up\nPROGRAM_READY {\"windows\":[]}\n" + want
				}
				if out.String() != want {
					t.Fatalf("stdout = %q, want %q", out.String(), want)
				}
			case <-time.After(time.Second):
				// Close and join the reader even on the failing-first tree.
				local.Close()
				<-done
				t.Fatal("guest ERR waited for EOF instead of refusing promptly")
			}
		})
	}
}

func TestGuestErrorSupervision(t *testing.T) {
	local, guest := net.Pipe()
	defer guest.Close()
	_ = guest.SetDeadline(time.Now().Add(2 * time.Second))
	var out bytes.Buffer
	done := make(chan error, 1)
	go func() {
		// Fake QEMU and relay peers remain running; only the control error
		// should end boot supervision and reach boot's cleanup defers.
		done <- superviseGuest(context.Background(), local, &out, nil, nil, nil)
	}()
	if _, err := io.WriteString(guest, "SRF1 ERR program stack exited\n"); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		var refusal *proto.Error
		if !errors.As(err, &refusal) || refusal.Code != 4 {
			t.Fatalf("boot status: %v", err)
		}
		if out.String() != "REFUSED: guest program stack exited\n" {
			t.Fatalf("stdout = %q", out.String())
		}
		// Supervision closes and joins its reader before returning.
		if _, err := guest.Write([]byte("unexpected")); err == nil {
			t.Fatal("control peer remained open")
		}
	case <-time.After(time.Second):
		local.Close()
		<-done
		t.Fatal("guest refusal did not end boot supervision promptly")
	}
}

func TestControlDisconnectStatus(t *testing.T) {
	local, guest := net.Pipe()
	guest.Close()
	err := superviseGuest(context.Background(), local, io.Discard, nil, nil, nil)
	var refusal *proto.Error
	if !errors.As(err, &refusal) || refusal.Code != 6 {
		t.Fatalf("disconnect status: %v", err)
	}
}

func TestGuestErrorDuringPeerShutdown(t *testing.T) {
	local, guest := net.Pipe()
	defer guest.Close()
	_ = guest.SetDeadline(time.Now().Add(2 * time.Second))
	peerDone := make(chan struct{})
	close(peerDone)
	done := make(chan error, 1)
	var out bytes.Buffer
	go func() {
		done <- superviseGuest(context.Background(), local, &out, nil, peerDone, nil)
	}()
	// The agent port may close as the guest powers off immediately after ERR.
	if _, err := io.WriteString(guest, "SRF1 ERR program stack exited\n"); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		var refusal *proto.Error
		if !errors.As(err, &refusal) || refusal.Code != 4 {
			t.Fatalf("guest error lost to peer shutdown: %v", err)
		}
		if out.String() != "REFUSED: guest program stack exited\n" {
			t.Fatalf("stdout = %q", out.String())
		}
	case <-time.After(time.Second):
		local.Close()
		<-done
		t.Fatal("guest shutdown stalled supervision")
	}
}

func TestGuestErrorWriteFailure(t *testing.T) {
	for _, tc := range []struct {
		name string
		w    io.Writer
		want error
	}{
		{"short write", writerFunc(func([]byte) (int, error) { return 0, nil }), io.ErrShortWrite},
		{"writer error", writerFunc(func([]byte) (int, error) { return 0, io.ErrClosedPipe }), io.ErrClosedPipe},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := relayControl(bytes.NewBufferString("SRF1 ERR program stack exited\n"), tc.w); !errors.Is(err, tc.want) {
				t.Fatalf("write failure: %v, want %v", err, tc.want)
			}
		})
	}
}
