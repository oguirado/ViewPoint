// Package tiling deterministically plans browser windows on a single display.
// It does not start browsers, read profiles, capture frames, or inject input.
package tiling

import "fmt"

const (
	MaxDisplayWidth  = 7680
	MaxDisplayHeight = 4320
	MaxWindows       = 16
	DefaultMinWidth  = 1264
	DefaultMinHeight = 900
)

// ErrorKind classifies a refused request or point mapping.
type ErrorKind string

const (
	InvalidInput      ErrorKind = "invalid_input"
	InsufficientSpace ErrorKind = "insufficient_space"
	OutsideWindow     ErrorKind = "outside_window"
	UnknownSurface    ErrorKind = "unknown_surface"
)

// Error is a typed refusal. Callers can inspect Kind via errors.As.
type Error struct {
	Kind   ErrorKind
	Detail string
}

func (e *Error) Error() string { return string(e.Kind) + ": " + e.Detail }

func refuse(kind ErrorKind, format string, args ...any) error {
	return &Error{Kind: kind, Detail: fmt.Sprintf(format, args...)}
}

// Spec specifies one identity's browser window layout. Zero minimum dimensions
// select DefaultMinWidth and DefaultMinHeight independently.
type Spec struct {
	ProfileID     string
	DisplayWidth  int
	DisplayHeight int
	Windows       int
	MinWidth      int
	MinHeight     int
}

// Point is an integer pixel coordinate, interpreted in the relevant space.
type Point struct {
	X, Y int
}

// Rect is a half-open rectangle [X, X+Width) x [Y, Y+Height).
type Rect struct {
	X, Y          int
	Width, Height int
}

// Contains reports whether a point lies in the half-open rectangle.
func (r Rect) Contains(p Point) bool {
	return p.X >= r.X && p.Y >= r.Y && p.X-r.X < r.Width && p.Y-r.Y < r.Height
}

// Window describes a planned Chromium window, without any profile contents.
type Window struct {
	SurfaceID string
	Rect      Rect
}

// ChromiumArgs returns a new argv fragment for the window (not a process
// command line, and never containing profile or credential paths).
func (w Window) ChromiumArgs() []string {
	return []string{
		fmt.Sprintf("--window-position=%d,%d", w.Rect.X, w.Rect.Y),
		fmt.Sprintf("--window-size=%d,%d", w.Rect.Width, w.Rect.Height),
	}
}

// Layout contains row-major windows of one identity on one display. All
// rectangles are non-overlapping. A partially populated grid has empty cells.
type Layout struct {
	DisplayWidth, DisplayHeight int
	Columns, Rows               int
	Windows                     []Window
}

// Plan builds the widest possible row-major grid given minimum cell sizes:
// columns = min(N, floor(displayWidth / minWidth)), rows = ceil(N / columns).
// Each cell gets the floored equal size; the final column/row receives the
// remainder pixels. Unused cells remain empty and are not addressable.
func Plan(spec Spec) (Layout, error) {
	var layout Layout
	if !validProfileID(spec.ProfileID) {
		return layout, refuse(InvalidInput, "profile_id must start with an ASCII letter or digit and contain only letters, digits, dot, underscore or hyphen")
	}
	if spec.DisplayWidth < 1 || spec.DisplayHeight < 1 ||
		spec.DisplayWidth > MaxDisplayWidth || spec.DisplayHeight > MaxDisplayHeight {
		return layout, refuse(InvalidInput, "display must be between 1x1 and %dx%d", MaxDisplayWidth, MaxDisplayHeight)
	}
	if spec.Windows < 1 || spec.Windows > MaxWindows {
		return layout, refuse(InvalidInput, "window count must be 1..%d", MaxWindows)
	}
	if spec.MinWidth < 0 || spec.MinHeight < 0 {
		return layout, refuse(InvalidInput, "minimum window dimensions cannot be negative")
	}
	minW, minH := spec.MinWidth, spec.MinHeight
	if minW == 0 {
		minW = DefaultMinWidth
	}
	if minH == 0 {
		minH = DefaultMinHeight
	}
	maxCols := spec.DisplayWidth / minW
	if maxCols < 1 {
		return layout, refuse(InsufficientSpace, "display width %d is below minimum window width %d", spec.DisplayWidth, minW)
	}
	cols := min(spec.Windows, maxCols)
	rows := (spec.Windows + cols - 1) / cols
	if spec.DisplayHeight/rows < minH {
		return layout, refuse(InsufficientSpace, "%d windows require a %d-column by %d-row grid with minimum %dx%d cells, display is %dx%d", spec.Windows, cols, rows, minW, minH, spec.DisplayWidth, spec.DisplayHeight)
	}
	cellW, extraW := spec.DisplayWidth/cols, spec.DisplayWidth%cols
	cellH, extraH := spec.DisplayHeight/rows, spec.DisplayHeight%rows
	layout = Layout{
		DisplayWidth: spec.DisplayWidth, DisplayHeight: spec.DisplayHeight,
		Columns: cols, Rows: rows, Windows: make([]Window, 0, spec.Windows),
	}
	for i := 0; i < spec.Windows; i++ {
		col, row := i%cols, i/cols
		w, h := cellW, cellH
		if col == cols-1 {
			w += extraW
		}
		if row == rows-1 {
			h += extraH
		}
		layout.Windows = append(layout.Windows, Window{
			SurfaceID: fmt.Sprintf("%s/w%d", spec.ProfileID, i+1),
			Rect:      Rect{X: col * cellW, Y: row * cellH, Width: w, Height: h},
		})
	}
	return layout, nil
}

func validProfileID(id string) bool {
	if len(id) == 0 {
		return false
	}
	for i := 0; i < len(id); i++ {
		c := id[i]
		letter := c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
		digit := c >= '0' && c <= '9'
		if letter || digit || (i != 0 && (c == '-' || c == '_' || c == '.')) {
			continue
		}
		return false
	}
	return true
}

// DisplayToWindow maps a display pixel to a surface and its window-local
// coordinate. Pixels in unused grid cells or outside the display are refused.
func (l Layout) DisplayToWindow(p Point) (string, Point, error) {
	for _, w := range l.Windows {
		if w.Rect.Contains(p) {
			return w.SurfaceID, Point{X: p.X - w.Rect.X, Y: p.Y - w.Rect.Y}, nil
		}
	}
	return "", Point{}, refuse(OutsideWindow, "display point (%d,%d) is outside every planned window", p.X, p.Y)
}

// WindowToDisplay maps a window-local pixel back to display coordinates.
// Unknown surface ids and points outside the selected window are refused.
func (l Layout) WindowToDisplay(surfaceID string, p Point) (Point, error) {
	for _, w := range l.Windows {
		if w.SurfaceID != surfaceID {
			continue
		}
		if p.X < 0 || p.Y < 0 || p.X >= w.Rect.Width || p.Y >= w.Rect.Height {
			return Point{}, refuse(OutsideWindow, "local point (%d,%d) is outside surface %s", p.X, p.Y, surfaceID)
		}
		return Point{X: w.Rect.X + p.X, Y: w.Rect.Y + p.Y}, nil
	}
	return Point{}, refuse(UnknownSurface, "surface %q is not part of this layout", surfaceID)
}
