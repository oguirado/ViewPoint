//go:build cockpit

// The OpenSuperficies cockpit: the only cgo artifact of the campaign (DEC-16). It is linked with
// `wails build -m -tags 'cockpit webkit2_41'` and talks to srfd only over the UDS (see app.go).
package main

import (
	"embed"
	"fmt"
	"os"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
)

//go:embed all:frontend/dist
var assets embed.FS

// The 1/6 tile (spec 1.2): 3840/3 by (2160-32)/2. cockpit/frontend/src/lib/geometria.ts is the authority for
// these figures; tests/geometria.test.ts requires them to match.
const (
	ventanaAncho = 1280
	ventanaAlto  = 1064
)

func main() {
	app := NewApp()
	err := wails.Run(&options.App{
		Title:            "OpenSuperficies Cockpit",
		Width:            ventanaAncho,
		Height:           ventanaAlto,
		AssetServer:      &assetserver.Options{Assets: assets},
		BackgroundColour: &options.RGBA{R: 7, G: 9, B: 14, A: 255}, // --canvas-void
		OnStartup:        app.startup,
		OnShutdown:       app.shutdown,
		Bind:             []interface{}{app},
		Menu:             recMenu(app),
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-cockpit: %v\n", err)
		os.Exit(2)
	}
}
