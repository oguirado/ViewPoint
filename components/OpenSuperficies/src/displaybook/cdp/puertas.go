package cdp

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// Standard taxonomy exit codes (PLAN v3.7 §5, DEC-5).
const (
	RCOk       = 0
	RCLimpio   = 1
	RCUso      = 2
	RCSustrato = 3
	RCSinFin   = 4
	RCOcupada  = 5
	RCAmbiguo  = 6
	RCHumano   = 7
	RCAbortada = 8
	RCNoAuth   = 9
)

// ErrHumano indicates an interactive challenge or missing login requires operator attention (rc=7).
type ErrHumano struct {
	Msg string
}

func (e *ErrHumano) Error() string {
	return e.Msg
}

// ErrAmbiguo indicates submission state is ambiguous and may have been charged (rc=6). Never retried.
type ErrAmbiguo struct {
	Msg string
}

func (e *ErrAmbiguo) Error() string {
	return e.Msg
}

// EvaluarSalidaStream maps stream completion status to the normative exit code and error.
// If completo is false, it returns RCSinFin (rc=4) and an explanatory error.
// If completo is true, it returns RCOk (rc=0) and nil.
func EvaluarSalidaStream(completo bool) (int, error) {
	if !completo {
		return RCSinFin, fmt.Errorf("stream vigilado no completo satisfactoriamente")
	}
	return RCOk, nil
}

// GateAuth (G-AUTH): verifies the session is alive; halts if login CTA is visible.
func GateAuth(ctx context.Context, c *Client, sel map[string][]string) error {
	loginSels := sel["login_cta"]
	if len(loginSels) == 0 {
		loginSels = []string{"[data-testid=\"login-cta\"]", "#login-cta"}
	}
	s, err := c.PrimerSelectorVisible(ctx, loginSels)
	if err == nil && s != "" {
		return &ErrHumano{Msg: "G-AUTH: la sesion no esta iniciada en este perfil"}
	}
	return nil
}

// GateHuman (G-HUMAN): halts immediately if an interactive challenge/captcha is visible.
// Symmetric with G-AUTH and G-SUBMIT by requiring visible element with positive-area bounding rect.
func GateHuman(ctx context.Context, c *Client, sel map[string][]string) error {
	desafioSels := sel["desafio"]
	if len(desafioSels) == 0 {
		desafioSels = []string{
			"iframe[src*=\"challenges.cloudflare.com\"]",
			"#challenge-running",
			"[id*=\"turnstile\" i]",
		}
	}
	s, err := c.PrimerSelectorVisible(ctx, desafioSels)
	if err == nil && s != "" {
		return &ErrHumano{Msg: "G-HUMAN: desafio interactivo en pantalla; requiere al operador"}
	}
	return nil
}

// GateSubmit (G-SUBMIT): synchronously re-evaluates G-AUTH, G-HUMAN, and G-ATTACH preconditions,
// increments an in-page counter, clicks submit, and then polls for visible stream container or stop button.
// Returns true if visible stream region begins within timeout; returns false (ambiguous, rc=6) on timeout.
// The despachos counter is a required, non-variadic parameter, updated by reading back the in-page counter
// written at the single point where dispatch is actually attempted.
func GateSubmit(ctx context.Context, c *Client, sel map[string][]string, timeout time.Duration, despachos *int) (bool, error) {
	if despachos == nil {
		return false, fmt.Errorf("despachos counter is required")
	}

	authSels := sel["login_cta"]
	if len(authSels) == 0 {
		authSels = []string{"[data-testid=\"login-cta\"]", "#login-cta"}
	}
	desafioSels := sel["desafio"]
	if len(desafioSels) == 0 {
		desafioSels = []string{
			"iframe[src*=\"challenges.cloudflare.com\"]",
			"#challenge-running",
			"[id*=\"turnstile\" i]",
		}
	}
	btnSels := sel["boton_enviar"]
	if len(btnSels) == 0 {
		btnSels = []string{"button[aria-label*=\"Submit\" i]", "button[type=\"submit\"]", "#submitBtn"}
	}
	inputSels := sel["input_fichero"]
	if len(inputSels) == 0 {
		inputSels = []string{"#fileInput", "[data-testid*=\"dropzone\"] input[type=\"file\"]", "input[type=\"file\"]"}
	}

	authJSON, _ := json.Marshal(authSels)
	desafioJSON, _ := json.Marshal(desafioSels)
	btnJSON, _ := json.Marshal(btnSels)
	inputJSON, _ := json.Marshal(inputSels)

	// Single synchronous Runtime.evaluate that recomputes every gate, increments in-page counter, and clicks submit
	dispatchExpr := fmt.Sprintf(`(() => {
		// 1. G-AUTH check
		const authSels = %s;
		for (const s of authSels) {
			const el = document.querySelector(s);
			if (el) {
				let vis = false;
				if (typeof el.checkVisibility === 'function') {
					vis = el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true});
				} else {
					const st = window.getComputedStyle(el);
					vis = (st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
				}
				if (vis) {
					const rects = el.getClientRects();
					if (rects && rects.length > 0) {
						for (let i = 0; i < rects.length; i++) {
							if (rects[i].width > 0 && rects[i].height > 0) {
								return { ok: false, gate: "G-AUTH", reason: "login-cta visible" };
							}
						}
					}
				}
			}
		}

		// 2. G-HUMAN check (symmetric with PrimerSelectorVisible: checkVisibility + positive-area rect)
		const desafioSels = %s;
		for (const s of desafioSels) {
			const el = document.querySelector(s);
			if (el) {
				let vis = false;
				if (typeof el.checkVisibility === 'function') {
					vis = el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true});
				} else {
					const st = window.getComputedStyle(el);
					vis = (st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0');
				}
				if (vis) {
					const rects = el.getClientRects();
					if (rects && rects.length > 0) {
						for (let i = 0; i < rects.length; i++) {
							if (rects[i].width > 0 && rects[i].height > 0) {
								return { ok: false, gate: "G-HUMAN", reason: "desafio interactivo visible" };
							}
						}
					}
				}
			}
		}

		// 3. G-ATTACH check (if file input exists, verify native FileList)
		const inputSels = %s;
		let fileInput = null;
		for (const s of inputSels) {
			const el = document.querySelector(s);
			if (el && el instanceof HTMLInputElement && el.type === 'file') {
				fileInput = el;
				break;
			}
		}
		if (!fileInput) {
			fileInput = document.querySelector('input[type="file"]');
		}
		if (fileInput) {
			const filesGetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files") ?
								Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files").get : null;
			const files = filesGetter ? Reflect.apply(filesGetter, fileInput, []) : fileInput.files;
			if (!files || files.length === 0) {
				return { ok: false, gate: "G-ATTACH", reason: "input[type=file] has empty FileList" };
			}
		}

		// 4. G-SUBMIT check & trigger
		const btnSels = %s;
		let submitter = null;
		for (const s of btnSels) {
			const el = document.querySelector(s);
			if (el) {
				submitter = el;
				break;
			}
		}
		if (!submitter) {
			return { ok: false, gate: "G-SUBMIT", reason: "submitter button missing" };
		}
		if (submitter.disabled) {
			return { ok: false, gate: "G-SUBMIT", reason: "submitter button disabled" };
		}
		if (typeof submitter.checkVisibility === 'function') {
			if (!submitter.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true})) {
				return { ok: false, gate: "G-SUBMIT", reason: "submitter button hidden" };
			}
		} else {
			const st = window.getComputedStyle(submitter);
			if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') {
				return { ok: false, gate: "G-SUBMIT", reason: "submitter button hidden" };
			}
		}
		const rects = submitter.getClientRects();
		let hasArea = false;
		if (rects && rects.length > 0) {
			for (let i = 0; i < rects.length; i++) {
				if (rects[i].width > 0 && rects[i].height > 0) {
					hasArea = true;
					break;
				}
			}
		}
		if (!hasArea) {
			return { ok: false, gate: "G-SUBMIT", reason: "submitter button has zero area" };
		}

		// IN-PAGE dispatch counter incremented immediately BEFORE submitter.click()
		window.__srf_despachos = (window.__srf_despachos || 0) + 1;
		const curDespachos = window.__srf_despachos;
		submitter.click();
		return { ok: true, despachos: curDespachos };
	})()`, authJSON, desafioJSON, inputJSON, btnJSON)

	raw, err := c.Evaluate(ctx, dispatchExpr)
	if err != nil {
		// If evaluate failed after potential click, attempt to read back in-page counter
		if rawCnt, errCnt := c.Evaluate(ctx, "window.__srf_despachos || 0"); errCnt == nil {
			var n int
			if json.Unmarshal(rawCnt, &n) == nil && n > 0 {
				*despachos = n
			}
		}
		return false, fmt.Errorf("dispatch evaluate error: %w", err)
	}

	var dRes struct {
		Ok        bool   `json:"ok"`
		Gate      string `json:"gate"`
		Reason    string `json:"reason"`
		Despachos int    `json:"despachos"`
	}
	if err := json.Unmarshal(raw, &dRes); err != nil {
		var b bool
		if json.Unmarshal(raw, &b) == nil && b {
			dRes.Ok = true
		} else {
			return false, fmt.Errorf("dispatch decode error: %w (raw: %s)", err, string(raw))
		}
	}

	if !dRes.Ok {
		if dRes.Gate == "G-HUMAN" {
			return false, &ErrHumano{Msg: "G-HUMAN: desafio interactivo en pantalla; requiere al operador"}
		}
		if dRes.Gate == "G-AUTH" {
			return false, &ErrHumano{Msg: "G-AUTH: la sesion no esta iniciada en este perfil"}
		}
		if dRes.Gate == "G-ATTACH" {
			return false, fmt.Errorf("G-ATTACH: precondicion fallida: %s", dRes.Reason)
		}
		return false, fmt.Errorf("G-SUBMIT precondicion fallida: %s", dRes.Reason)
	}

	// Read back in-page dispatch counter
	if dRes.Despachos > 0 {
		*despachos = dRes.Despachos
	} else {
		if rawCnt, errCnt := c.Evaluate(ctx, "window.__srf_despachos || 0"); errCnt == nil {
			var n int
			if json.Unmarshal(rawCnt, &n) == nil && n > 0 {
				*despachos = n
			} else {
				*despachos++
			}
		} else {
			*despachos++
		}
	}

	streamSels := sel["stream"]
	if len(streamSels) == 0 {
		streamSels = []string{"[data-testid=\"answer-stream\"]", "#stream", "#stream-container"}
	}
	detenerSels := sel["detener"]
	if len(detenerSels) == 0 {
		detenerSels = []string{"[data-testid=\"stop-generating\"]", "button[aria-label*=\"Stop\" i]", "#stopBtn"}
	}

	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		s1, _ := c.PrimerSelectorVisible(ctx, streamSels)
		s2, _ := c.PrimerSelectorVisible(ctx, detenerSels)
		if s1 != "" || s2 != "" {
			return true, nil
		}
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
	return false, nil
}
