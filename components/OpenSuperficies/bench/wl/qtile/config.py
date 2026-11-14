# Deterministic Qtile profile for the WL manifest (data only; installing Qtile needs an image layer, W2).
from libqtile import layout
from libqtile.config import Screen

layouts = [layout.MonadTall(ratio=0.5, border_width=0, margin=0, change_ratio=0.0)]
screens = [Screen(top=None, bottom=None, left=None, right=None)]  # no bar, no clock
groups = []
follow_mouse_focus = False
cursor_warp = False
auto_fullscreen = False
reconfigure_screens = False
wmname = "wl-qtile"
widget_defaults = dict(font="DejaVu Sans Mono", fontsize=14)
# animations: none (Qtile has no compositor animations); fonts fixed above.
