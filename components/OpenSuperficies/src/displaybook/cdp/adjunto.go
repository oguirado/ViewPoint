package cdp

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/chromedp/cdproto/dom"
)

var genericPayloadRegex = regexp.MustCompile(`(?i)paste\.txt|pasted[- ]text|untitled`)

// GateAttachStatus captures the DOM state evaluated during G-ATTACH settling.
type GateAttachStatus struct {
	Texto     string   `json:"texto"`
	Errores   []string `json:"errores"`
	EdLen     int      `json:"edLen"`
	Pendiente bool     `json:"pendiente"`
	ChipFound bool     `json:"chipFound"`
	ChipAria  string   `json:"chipAria"`
	ChipTitle string   `json:"chipTitle"`
	ChipText  string   `json:"chipText"`
}

// GateAttach verifies the four mandatory attachment conditions before dispatch:
// 1. No upload error alerts.
// 2. Payload has not degraded to generic paste.txt / pasted-text / untitled.
// 3. Editor has not absorbed the payload (> 4000 characters).
// 4. Chip resolution: resolves by aria-label and title before innerText (OBJ-6, HAL-U13-37).
func GateAttach(ctx context.Context, c *Client, sel map[string][]string, nombre string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	inputSels := sel["input_fichero"]
	if len(inputSels) == 0 {
		inputSels = []string{"#fileInput", "[data-testid*=\"dropzone\"] input[type=\"file\"]", "input[type=\"file\"]"}
	}
	inputJSON, _ := json.Marshal(inputSels)

	expr := fmt.Sprintf(`(() => {
		const t = document.body.innerText || "";
		const errores = Array.from(document.querySelectorAll(
			'[role="alert"],[aria-invalid="true"],[data-error],[class*="error" i]'))
			.map(n => (n.innerText||"").trim()).filter(Boolean);

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
		let hasValidFile = false;
		if (fileInput) {
			const filesGetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files") ?
								Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files").get : null;
			const files = filesGetter ? Reflect.apply(filesGetter, fileInput, []) : fileInput.files;
			if (files && files.length > 0) {
				hasValidFile = true;
			}
		}
		if (fileInput && !hasValidFile) {
			errores.push("input[type=file] has empty FileList");
		}

		const ed = document.querySelector('textarea,[contenteditable="true"],[role="textbox"]');
		const edLen = ed ? ((ed.value ?? ed.innerText ?? "").length) : 0;
		const target = %q;
		const chips = Array.from(document.querySelectorAll(
			'.chip, #chipContainer > *, [data-testid*="attachment"], [class*="attachment" i]'));
		let chipFound = false;
		let chipAria = "";
		let chipTitle = "";
		let chipText = "";
		for (const ch of chips) {
			const aria = ch.getAttribute('aria-label') || '';
			const title = ch.getAttribute('title') || '';
			const it = (ch.innerText || '').trim();
			if (aria.includes(target) || title.includes(target)) {
				chipFound = true;
				chipAria = aria;
				chipTitle = title;
				chipText = it;
				break;
			}
			if (it.includes(target)) {
				chipFound = true;
				chipText = it;
				break;
			}
		}
		return {
			texto: t.slice(0, 200000),
			errores: errores,
			edLen: edLen,
			pendiente: /uploading|subiendo|processing|0%%/i.test(t),
			chipFound: chipFound,
			chipAria: chipAria,
			chipTitle: chipTitle,
			chipText: chipText
		};
	})()`, inputJSON, nombre)

	for {
		raw, err := c.Evaluate(ctx, expr)
		if err != nil {
			return fmt.Errorf("G-ATTACH evaluate error: %w", err)
		}
		var st GateAttachStatus
		if err := json.Unmarshal(raw, &st); err != nil {
			return fmt.Errorf("G-ATTACH decode error: %w", err)
		}

		if len(st.Errores) > 0 {
			return fmt.Errorf("G-ATTACH: error de subida -> %s", strings.Join(st.Errores, " | "))
		}
		if genericPayloadRegex.MatchString(st.Texto) || genericPayloadRegex.MatchString(st.ChipText) {
			return fmt.Errorf("G-ATTACH: el payload degrado a un adjunto generico")
		}
		if st.EdLen > 4000 {
			return fmt.Errorf("G-ATTACH: %d caracteres sentados en el editor", st.EdLen)
		}
		if st.ChipFound && !st.Pendiente {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("G-ATTACH: el chip '%s' nunca se asento", nombre)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// Adjuntar sets file input files and verifies G-ATTACH settlement.
func Adjuntar(ctx context.Context, c *Client, sel map[string][]string, rutaNavegador, nombre string, timeout time.Duration) error {
	inputSels := sel["input_fichero"]
	if len(inputSels) == 0 {
		inputSels = []string{"#fileInput", "[data-testid*=\"dropzone\"] input[type=\"file\"]", "input[type=\"file\"]"}
	}
	var selector string
	deadlineSel := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadlineSel) {
		var sErr error
		selector, sErr = c.PrimerSelector(ctx, inputSels)
		if sErr == nil && selector != "" {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(150 * time.Millisecond):
		}
	}
	if selector == "" {
		return fmt.Errorf("no se encontro ningun input[type=file] (literal caduco?)")
	}

	doc, err := dom.GetDocument().Do(c.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("DOM.getDocument error: %w", err)
	}

	nodeID, err := dom.QuerySelector(doc.NodeID, selector).Do(c.WithContext(ctx))
	if err != nil || nodeID == 0 {
		return fmt.Errorf("DOM.querySelector para %q fallo: %w", selector, err)
	}

	if err := dom.SetFileInputFiles([]string{rutaNavegador}).WithNodeID(nodeID).Do(c.WithContext(ctx)); err != nil {
		return fmt.Errorf("DOM.setFileInputFiles fallo: %w", err)
	}

	return GateAttach(ctx, c, sel, nombre, timeout)
}
