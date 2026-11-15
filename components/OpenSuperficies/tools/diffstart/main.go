package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	"opensuperficies.local/srf/src/cubook/exec"
	"opensuperficies.local/srf/src/cubook/rc"
)

type goResult struct {
	OK       bool   `json:"ok"`
	T0Ns     int64  `json:"t0_ns,omitempty"`
	W        int    `json:"w,omitempty"`
	H        int    `json:"h,omitempty"`
	RunNonce string `json:"run_nonce,omitempty"`
	Error    string `json:"error,omitempty"`
}

func main() {
	var payloads []string
	decoder := json.NewDecoder(os.Stdin)
	if err := decoder.Decode(&payloads); err != nil {
		fmt.Fprintf(os.Stderr, "diffstart: invalid input json array: %v\n", err)
		os.Exit(rc.Usage)
	}

	encoder := json.NewEncoder(os.Stdout)
	for _, payloadStr := range payloads {
		b, err := base64.StdEncoding.DecodeString(payloadStr)
		if err != nil {
			if err := encoder.Encode(goResult{OK: false, Error: "E_SCHEMA"}); err != nil {
				fmt.Fprintf(os.Stderr, "diffstart: stdout write error: %v\n", err)
				os.Exit(rc.Usage)
			}
			continue
		}

		if len(b) == 0 || len(b) > 4096 {
			if err := encoder.Encode(goResult{OK: false, Error: "E_GATE"}); err != nil {
				fmt.Fprintf(os.Stderr, "diffstart: stdout write error: %v\n", err)
				os.Exit(rc.Usage)
			}
			continue
		}

		si, err := exec.ParseStartInfo(b)
		if err != nil {
			errCode := "E_SCHEMA"
			var rce *rc.Error
			if errors.As(err, &rce) {
				errCode = rce.Name
			}
			if err := encoder.Encode(goResult{OK: false, Error: errCode}); err != nil {
				fmt.Fprintf(os.Stderr, "diffstart: stdout write error: %v\n", err)
				os.Exit(rc.Usage)
			}
			continue
		}

		res := goResult{
			OK:       true,
			T0Ns:     si.T0Ns,
			W:        si.W,
			H:        si.H,
			RunNonce: si.RunNonce,
		}
		if err := encoder.Encode(res); err != nil {
			fmt.Fprintf(os.Stderr, "diffstart: stdout write error: %v\n", err)
			os.Exit(rc.Usage)
		}
	}
	_ = io.EOF
}
