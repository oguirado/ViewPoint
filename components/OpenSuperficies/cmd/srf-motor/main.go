package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"opensuperficies.local/srf/src/displaybook/browser"
	"opensuperficies.local/srf/src/displaybook/cdp"
	"opensuperficies.local/srf/src/displaybook/puertos"
	"opensuperficies.local/srf/src/dockerapi"
)

type MotorResult struct {
	RC        int            `json:"rc"`
	Gates     map[string]any `json:"gates"`
	Despachos int            `json:"despachos"`
	Chars     int            `json:"chars,omitempty"`
	Completo  bool           `json:"completo"`
	Error     string         `json:"error,omitempty"`
	Slot      uint16         `json:"slot,omitempty"`
	Puerto    uint16         `json:"puerto,omitempty"`
	Fixture   string         `json:"fixture,omitempty"`
	Orden     []string       `json:"orden,omitempty"`
}

func main() {
	var (
		slotFlag    uint
		fixtureFlag string
		jsonFlag    bool
		puertoFlag  uint
		tokenFlag   string
		promptFlag  string
		adjuntoFlag string
		nombreFlag  string
	)

	flag.UintVar(&slotFlag, "slot", 1, "slot number")
	flag.StringVar(&fixtureFlag, "fixture", "", "path to fixture HTML file")
	flag.BoolVar(&jsonFlag, "json", false, "output JSON report")
	flag.UintVar(&puertoFlag, "puerto", 0, "CDP port override (default 9220 + slot)")
	flag.StringVar(&tokenFlag, "token", "", "bearer authorization token override")
	flag.StringVar(&promptFlag, "prompt", "Consulta de prueba sobre fixture", "prompt text")
	flag.StringVar(&adjuntoFlag, "adjunto", "", "attachment file path")
	flag.StringVar(&nombreFlag, "nombre", "", "attachment name")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	slot := uint16(slotFlag)
	cdpPort := uint16(puertoFlag)
	if cdpPort == 0 {
		plan := puertos.NuevoPlanPuertos()
		par, err := plan.Par(slot)
		if err == nil {
			cdpPort = par.CDP
		} else {
			cdpPort = 9220 + slot
		}
	}

	token := tokenFlag
	if token == "" {
		tok, err := browser.Token(ctx, slot)
		if err == nil && tok != "" {
			token = tok
		} else {
			statePath := filepath.Join(browser.DefaultRuntimeDir, fmt.Sprintf("slot-%d.json", slot))
			if data, err := os.ReadFile(statePath); err == nil {
				var info browser.CapsulaInfo
				if json.Unmarshal(data, &info) == nil && info.Token != "" {
					token = info.Token
				}
			}
		}
	}

	res := MotorResult{
		RC:        cdp.RCOk,
		Gates:     make(map[string]any),
		Despachos: 0,
		Slot:      slot,
		Puerto:    cdpPort,
		Fixture:   fixtureFlag,
	}

	endpoint := fmt.Sprintf("http://127.0.0.1:%d", cdpPort)
	client, err := cdp.Connect(ctx, endpoint, token)
	if err != nil {
		res.RC = cdp.RCSustrato
		res.Error = fmt.Sprintf("CDP connect failed: %v", err)
		outputResult(res, jsonFlag)
		return
	}
	defer client.Close()

	sel := map[string][]string{
		"input_fichero": {"#fileInput", "[data-testid*=\"dropzone\"] input[type=\"file\"]", "input[type=\"file\"]"},
		"chip_adjunto":  {".chip", "#chipContainer", "[data-testid=\"attachment-chip\"]", "[class*=\"attachment\" i]"},
		"editor":        {"#editor", "textarea", "div[contenteditable=\"true\"]", "[role=\"textbox\"]"},
		"boton_enviar":  {"#submitBtn", "button[aria-label*=\"Submit\" i]", "button[type=\"submit\"]"},
		"stream":        {"#stream", "#stream-container", "[data-testid=\"answer-stream\"]"},
		"detener":       {"#stopBtn", "[data-testid=\"stop-generating\"]", "button[aria-label*=\"Stop\" i]"},
		"acciones_fin":  {"#actions", "[data-testid=\"answer-actions\"]"},
		"citas":         {"a", "a[data-testid=\"citation\"]"},
		"login_cta":     {"#login-cta", "[data-testid=\"login-cta\"]"},
		"desafio":       {"#challenge-running", "iframe[src*=\"challenges.cloudflare.com\"]", "[id*=\"turnstile\" i]"},
	}

	if fixtureFlag != "" {
		fixtureBytes, err := os.ReadFile(fixtureFlag)
		if err != nil {
			res.RC = cdp.RCUso
			res.Error = fmt.Sprintf("read fixture %s failed: %v", fixtureFlag, err)
			outputResult(res, jsonFlag)
			return
		}

		ctrName := fmt.Sprintf("srf-ng-%d", slot)
		b64HTML := base64.StdEncoding.EncodeToString(fixtureBytes)
		copyCmd := fmt.Sprintf("mkdir -p /uploads /tmp && echo '%s' | base64 -d > /uploads/fixture.html && cp /uploads/fixture.html /tmp/fixture.html && echo '# test fixture' > /uploads/test-fixture.md && cp /uploads/test-fixture.md /tmp/test-fixture.md", b64HTML)

		dockerClient, dErr := dockerapi.NewClient(browser.DefaultDockerSocket)
		if dErr != nil {
			res.RC = cdp.RCSustrato
			res.Error = fmt.Sprintf("docker client init failed: %v", dErr)
			outputResult(res, jsonFlag)
			return
		}
		if execErr := dockerClient.ExecRun(ctx, ctrName, []string{"sh", "-c", copyCmd}, io.Discard, io.Discard); execErr != nil {
			res.RC = cdp.RCSustrato
			res.Error = fmt.Sprintf("fixture copy into container %s failed: %v", ctrName, execErr)
			outputResult(res, jsonFlag)
			return
		}

		navURL := "file:///uploads/fixture.html"
		waitTimeout := 5 * time.Second
		if err := client.NavigateAndWait(ctx, navURL, waitTimeout); err != nil {
			dataURL := "data:text/html;base64," + b64HTML
			if err2 := client.NavigateAndWait(ctx, dataURL, waitTimeout); err2 != nil {
				res.RC = cdp.RCSustrato
				res.Error = fmt.Sprintf("wait for page ready failed: %v", err2)
				outputResult(res, jsonFlag)
				return
			}
		}

		// 1. G-AUTH
		res.Orden = append(res.Orden, "G-AUTH")
		if err := cdp.GateAuth(ctx, client, sel); err != nil {
			res.Gates["auth"] = false
			res.RC = cdp.RCHumano
			res.Error = err.Error()
			outputResult(res, jsonFlag)
			return
		}
		res.Gates["auth"] = true

		// 2. G-HUMAN
		res.Orden = append(res.Orden, "G-HUMAN")
		if err := cdp.GateHuman(ctx, client, sel); err != nil {
			res.Gates["human"] = false
			res.RC = cdp.RCHumano
			res.Error = err.Error()
			outputResult(res, jsonFlag)
			return
		}
		res.Gates["human"] = true

		// 3. G-ATTACH
		res.Orden = append(res.Orden, "G-ATTACH")
		attachTimeout := 4 * time.Second
		nombre := "test-fixture.md"
		if nombreFlag != "" {
			nombre = nombreFlag
		}
		ficheroRuta := "/uploads/test-fixture.md"
		if adjuntoFlag != "" {
			ficheroRuta = adjuntoFlag
		}

		if err := cdp.Adjuntar(ctx, client, sel, ficheroRuta, nombre, attachTimeout); err != nil {
			res.Gates["attach"] = false
			res.RC = cdp.RCLimpio
			res.Error = err.Error()
			outputResult(res, jsonFlag)
			return
		}
		res.Gates["attach"] = true

		// 4. Type Prompt into Editor
		editorSel, err := client.PrimerSelector(ctx, sel["editor"])
		if err != nil {
			res.RC = cdp.RCSustrato
			res.Error = fmt.Sprintf("editor selector evaluation failed: %v", err)
			outputResult(res, jsonFlag)
			return
		}
		if editorSel != "" {
			if _, err := client.Evaluate(ctx, fmt.Sprintf("document.querySelector(%q).focus()", editorSel)); err != nil {
				res.RC = cdp.RCSustrato
				res.Error = fmt.Sprintf("editor focus failed: %v", err)
				outputResult(res, jsonFlag)
				return
			}
			if err := client.InsertText(ctx, promptFlag); err != nil {
				res.RC = cdp.RCSustrato
				res.Error = fmt.Sprintf("editor insert text failed: %v", err)
				outputResult(res, jsonFlag)
				return
			}
			time.Sleep(150 * time.Millisecond)
		}

		// 5. G-SUBMIT
		res.Orden = append(res.Orden, "G-SUBMIT")
		submitTimeout := 5 * time.Second
		submitOk, subErr := cdp.GateSubmit(ctx, client, sel, submitTimeout, &res.Despachos)
		if subErr != nil || !submitOk {
			var errHumano *cdp.ErrHumano
			if errors.As(subErr, &errHumano) {
				if strings.Contains(errHumano.Error(), "G-HUMAN") {
					res.Gates["human"] = false
				} else if strings.Contains(errHumano.Error(), "G-AUTH") {
					res.Gates["auth"] = false
				}
				res.Gates["submit"] = false
				res.RC = cdp.RCHumano
				res.Error = subErr.Error()
				outputResult(res, jsonFlag)
				return
			}
			res.Gates["submit"] = false
			res.RC = cdp.RCAmbiguo
			if subErr != nil {
				res.Error = subErr.Error()
			} else {
				res.Error = "G-SUBMIT ambiguo: pudo enviarse y cobrarse"
			}
			outputResult(res, jsonFlag)
			return
		}
		res.Gates["submit"] = true

		// 6. Vigilance of Stream Output
		streamText, completo := vigilarStream(ctx, client, sel, 6*time.Second)
		res.Chars = len(streamText)
		res.Completo = completo
		rcStream, errStream := cdp.EvaluarSalidaStream(completo)
		res.RC = rcStream
		if errStream != nil {
			res.Error = errStream.Error()
		}
		outputResult(res, jsonFlag)
		return
	}

	// Normal production mode if fixtureFlag is empty
	if fixtureFlag == "" {
		res.RC = cdp.RCUso
		res.Completo = false
		res.Error = "fixture flag requerida para ejecucion del motor"
		outputResult(res, jsonFlag)
		return
	}
	outputResult(res, jsonFlag)
}

func vigilarStream(ctx context.Context, client *cdp.Client, sel map[string][]string, budget time.Duration) (string, bool) {
	streamSels := sel["stream"]
	selStream, _ := client.PrimerSelector(ctx, streamSels)
	if selStream == "" && len(streamSels) > 0 {
		selStream = streamSels[0]
	}

	detenerSels := sel["detener"]
	accionesSels := sel["acciones_fin"]

	lastLen := -1
	lastChange := time.Now()
	deadline := time.Now().Add(budget)

	for time.Now().Before(deadline) {
		expr := fmt.Sprintf(`(document.querySelectorAll(%q).length ? Array.from(document.querySelectorAll(%q)).pop().innerText : '')`, selStream, selStream)
		raw, _ := client.Evaluate(ctx, expr)
		var text string
		_ = json.Unmarshal(raw, &text)

		if len(text) != lastLen {
			lastLen = len(text)
			lastChange = time.Now()
		}

		stopBtn, _ := client.PrimerSelector(ctx, detenerSels)
		stopped := (stopBtn == "")

		actionsElem, _ := client.PrimerSelector(ctx, accionesSels)
		hasActions := (actionsElem != "")

		if stopped && (hasActions || time.Since(lastChange) > 4*time.Second) && len(text) > 0 {
			return text, true
		}

		select {
		case <-ctx.Done():
			return text, false
		case <-time.After(300 * time.Millisecond):
		}
	}

	expr := fmt.Sprintf(`(document.querySelectorAll(%q).length ? Array.from(document.querySelectorAll(%q)).pop().innerText : '')`, selStream, selStream)
	raw, _ := client.Evaluate(ctx, expr)
	var text string
	_ = json.Unmarshal(raw, &text)
	return text, false
}

func outputResult(res MotorResult, asJSON bool) {
	if asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(res)
	} else {
		fmt.Printf("rc: %d despachos: %d error: %s\n", res.RC, res.Despachos, res.Error)
	}
}
