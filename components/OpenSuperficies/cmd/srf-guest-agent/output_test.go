package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"testing"
	"time"

	"opensuperficies.local/srf/src/vmsurface/proto"
)

const pairSize = 9219368

func pairBytes() []byte {
	b := make([]byte, pairSize)
	for i := range b {
		b[i] = byte(i*31 + i/251)
	}
	leg := (len(b) - 16) / 2
	binary.BigEndian.PutUint64(b[:8], uint64(leg))
	binary.BigEndian.PutUint64(b[8+leg:16+leg], uint64(leg))
	return b
}

type slowReader struct{ io.Reader }

func (r slowReader) Read(p []byte) (int, error) {
	if len(p) > 16384 {
		p = p[:16384]
	}
	n, err := r.Reader.Read(p)
	if n > 0 {
		time.Sleep(time.Millisecond)
	}
	return n, err
}

func TestPairDeliveredBeforeExit(t *testing.T) {
	c, _ := start(t, "pair")
	c.SetDeadline(time.Now().Add(6 * time.Second))
	b, _ := json.Marshal(proto.Open{Argv: []string{"srf-fbread", "pair", ":100", "0"}})
	send(t, c, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: b})
	var out, stderr bytes.Buffer
	fullChunks := 0
	r := slowReader{c}
	for i := 0; i < pairSize+10; i++ {
		f, err := proto.ReadFrame(r)
		if err != nil {
			t.Fatal(err)
		}
		if f.Stream != 1 {
			t.Fatalf("unexpected stream %d", f.Stream)
		}
		switch f.Type {
		case proto.TStdout:
			out.Write(f.Payload)
			if len(f.Payload) == proto.MaxPayload {
				fullChunks++
			}
		case proto.TStderr:
			stderr.Write(f.Payload)
		case proto.TExit:
			var x proto.Exit
			if err := json.Unmarshal(f.Payload, &x); err != nil {
				t.Fatal(err)
			}
			want := sha256.Sum256(pairBytes())
			got := sha256.Sum256(out.Bytes())
			if x.RC != 0 || out.Len() != pairSize || got != want || stderr.String() != "pair complete" {
				t.Fatalf("EXIT rc=%d bytes=%d sha256=%x want=%x stderr=%q", x.RC, out.Len(), got, want, stderr.String())
			}
			if fullChunks != pairSize/proto.MaxPayload {
				t.Fatalf("full payload chunks %d want %d", fullChunks, pairSize/proto.MaxPayload)
			}
			// An unrelated refusal fences all previous writes: nothing for stream 1 may follow EXIT.
			send(t, c, proto.Frame{Type: proto.TOpen, Stream: 2, Payload: []byte(`{"argv":["sh"]}`)})
			rc, _, _ := exit(t, c, 2)
			if rc != 2 {
				t.Fatalf("fence rc %d", rc)
			}
			return
		default:
			t.Fatalf("unexpected frame %d", f.Type)
		}
	}
	t.Fatal("no EXIT")
}

func TestOutputMaxPayloadChunks(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	b.SetDeadline(time.Now().Add(time.Second))
	payload := make([]byte, proto.MaxPayload+17)
	done := make(chan error, 1)
	go func() {
		_, err := (&output{context.Background(), &sender{conn: a}, 1, proto.TStdout}).Write(payload)
		done <- err
	}()
	for _, size := range []int{proto.MaxPayload, 17} {
		f, err := proto.ReadFrame(b)
		if err != nil {
			t.Fatal(err)
		}
		if len(f.Payload) != size {
			t.Fatalf("chunk %d want %d", len(f.Payload), size)
		}
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestOutputTimeoutCutsStream(t *testing.T) {
	c, _ := start(t, "slow-flood")
	openStream(t, c, 1, 30)
	for i := 0; i < 100000; i++ {
		f, err := proto.ReadFrame(c)
		if err != nil {
			t.Fatal(err)
		}
		if f.Type == proto.TExit {
			var x proto.Exit
			if err := json.Unmarshal(f.Payload, &x); err != nil {
				t.Fatal(err)
			}
			if x.RC != 6 {
				t.Fatalf("timeout rc %d", x.RC)
			}
			return
		}
	}
	t.Fatal("timeout did not terminate output")
}

func BenchmarkOutputNetPipe(b *testing.B) {
	payload := pairBytes()
	a, peer := net.Pipe()
	defer a.Close()
	defer peer.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	peer.SetDeadline(time.Now().Add(30 * time.Second))
	done := make(chan error, 1)
	go func() {
		for i := 0; i < b.N; i++ {
			total := 0
			for total < len(payload) {
				f, err := proto.ReadFrame(peer)
				if err != nil {
					done <- err
					return
				}
				total += len(f.Payload)
			}
		}
		done <- nil
	}()
	o := &output{ctx, &sender{conn: a}, 1, proto.TStdout}
	b.SetBytes(int64(len(payload)))
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := o.Write(payload); err != nil {
			b.Fatal(err)
		}
	}
	if err := <-done; err != nil {
		b.Fatal(err)
	}
}
