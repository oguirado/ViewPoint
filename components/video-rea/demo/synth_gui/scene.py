# SPDX-License-Identifier: AGPL-3.0-only

"""Authored widget geometry, navigation and off-screen painting."""

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 640, 360
PARENTS = {"settings": "main", "confirm": "detail"}
# A guarded exit names the source text field that must contain a value.
EXITS = {
    "main": {"project": ("detail", None), "file_menu": ("main_menu", None)},
    "main_menu": {"settings_item": ("settings", None)},
    "detail": {"save": ("main", "notes"), "back": ("main", None), "archive": ("confirm", None)},
    "settings": {"apply": ("main", None)},
    "confirm": {"cancel_archive": ("detail", None), "confirm_archive": ("main", None)},
}


@dataclass
class State:
    project: str
    workspace: str
    list_x: int
    notes: str = ""
    status: str = "Draft note"
    scroll_offset: int = 0
    note_min_length: int = 1


@dataclass(frozen=True)
class Widget:
    id: str
    kind: str
    slot: tuple[int, int, int, int]
    text: str = ""
    parent: str | None = None
    enabled: bool = True

    @property
    def center(self):
        x, y, w, h = self.slot
        return x + w // 2, y + h // 2


def scene(screen: str, state: State) -> list[Widget]:
    if screen == "settings":
        return [
            Widget("settings_dialog", "dialog", (113, 76, 414, 232), "Workspace settings"),
            Widget("workspace_label", "text", (135, 130, 300, 33), "Workspace name", "settings_dialog"),
            Widget("workspace", "text_field", (135, 168, 370, 46), state.workspace, "settings_dialog"),
            Widget("apply", "button", (374, 243, 131, 41), "Apply", "settings_dialog"),
        ]
    if screen == "confirm":
        return [
            Widget("archive_dialog", "dialog", (105, 93, 430, 185), "Archive project?"),
            Widget("archive_message", "text", (127, 143, 386, 38), state.project, "archive_dialog"),
            Widget("cancel_archive", "button", (127, 211, 140, 41), "Cancel", "archive_dialog"),
            Widget("confirm_archive", "button", (362, 211, 150, 41), "Confirm archive", "archive_dialog"),
        ]

    chrome = [Widget("app_title", "text", (20, 6, 160, 30), "Field Notebook")]
    if screen in {"main", "main_menu"}:
        x, offset = state.list_x, state.scroll_offset
        widgets = [
            Widget("file_menu", "menu", (24, 53, 90, 30), "File"),
            Widget("projects", "text", (24, 94, 240, 37), "Projects"),
            Widget("project_list", "list", (x, 131, 350, 171)),
            Widget("project", "button", (x+6, 157-offset, 290, 40), state.project, "project_list"),
            Widget("project_b", "text", (x+6, 210-offset, 290, 36), "Orchard inventory", "project_list"),
            Widget("project_c", "text", (x+6, 256-offset, 290, 36), "River transect", "project_list"),
            Widget("scroll_down", "button", (x+305, 253, 38, 39), "v", "project_list"),
            Widget("active_workspace", "text", (24, 312, 344, 31), state.workspace),
        ]
        if screen == "main_menu":
            widgets.extend((
                Widget("file_popover", "menu", (400, 57, 216, 144), "File menu", "file_menu"),
                Widget("settings_item", "menu_item", (410, 111, 196, 37), "Settings", "file_popover"),
            ))
        return chrome + widgets
    if screen == "detail":
        return chrome + [
            Widget("back", "button", (24, 54, 96, 34), "Back"),
            Widget("detail_title", "text", (24, 98, 400, 42), state.project),
            Widget("notes_label", "text", (24, 151, 220, 32), "Notes"),
            Widget("notes", "text_field", (24, 184, 568, 50), state.notes),
            Widget("save", "button", (24, 261, 128, 38), "Save note", enabled=bool(state.notes)),
            Widget("archive", "button", (466, 261, 126, 38), "Archive..."),
            Widget("save_status", "text", (24, 311, 250, 30), state.status),
        ]
    raise ValueError(f"Unknown screen: {screen}")


FILLS = {
    "menu": "#203c5b", "menu_item": "#2c5977", "button": "#155777",
    "text_field": "#ffffff", "dialog": "#f8fafc", "list": "#d6e2e9",
}


def paint_widget(draw, widget, font, hovered):
    x, y, w, h = widget.slot
    if widget.kind in FILLS:
        draw.rounded_rectangle(
            (x, y, x+w-1, y+h-1), radius=6,
            fill=FILLS[widget.kind] if widget.enabled else "#78858c",
            outline="#ed912a" if hovered else "#8cabc2", width=3 if hovered else 2,
        )
    if not widget.text:
        return None
    light = widget.id == "app_title" or widget.kind in {"menu", "menu_item", "button"}
    dx = x + 10
    dy = y + (13 if widget.id == "file_popover" or widget.kind == "dialog" else (h-14)//2)
    draw.text((dx, dy), widget.text, font=font, fill="#f8fafc" if light else "#15263e")

    mask, (ox, oy) = font.getmask2(widget.text, mode="L")
    left, top, right, bottom = mask.getbbox()
    return [dx + ox + left, dy + oy + top, right - left, bottom - top]


def render(screen: str, state: State, pointer, emphasis, hover_target, font: ImageFont.FreeTypeFont,
           *, layout=None):
    """Return the raster and the geometry painted into it, before cursor occlusion."""
    parent = PARENTS.get(screen)
    detail = (parent or screen) == "detail"
    if layout is None:
        size, header, radius, scrim = (WIDTH, HEIGHT), 43, 6, 50
        background, chrome = ("#e2eef0" if detail else "#e6eef3"), "#14253c"
        widgets_for, paint = scene, paint_widget
    else:
        size, header = layout.size, layout.header
        radius, scrim = layout.dialog_radius, layout.scrim_percent
        background, chrome = layout.palette["detail" if detail else "main"], layout.palette["chrome"]
        widgets_for, paint = layout.widgets, layout.paint_widget
    image = Image.new("RGB", size, background)
    ImageDraw.Draw(image).rectangle((0, 0, size[0]-1, header-1), fill=chrome)
    labels = []

    def layer(widgets, layer_name):
        draw = ImageDraw.Draw(image)
        for w in widgets:
            ink = paint(draw, w, font, w.id == hover_target)
            labels.append({
                "id": w.id, "kind": w.kind, "text": w.text,
                "slot_bbox": list(w.slot), "text_bbox": ink,
                "bbox": ink if w.kind == "text" else list(w.slot),
                "parent_id": w.parent, "child_ids": [child.id for child in widgets if child.parent == w.id],
                "enabled": w.enabled, "layer": layer_name, "modal_occlusion": "none",
            })

    foreground = widgets_for(screen, state)
    if parent:
        layer(widgets_for(parent, state), "parent")
        image = image.point([i * scrim // 100 for i in range(256)] * 3)
        dialog = next(w for w in foreground if w.kind == "dialog")
        x, y, w, h = dialog.slot
        mask = Image.new("L", image.size)
        ImageDraw.Draw(mask).rounded_rectangle((x, y, x+w-1, y+h-1), radius=radius, fill=255)
        for label in labels:
            x, y, w, h = label["bbox"]
            low, high = mask.crop((x, y, x+w, y+h)).getextrema()
            if high:
                label["modal_occlusion"] = "full" if low else "partial"
    layer(foreground, "foreground")
    draw = ImageDraw.Draw(image)
    x, y = pointer
    if emphasis in {"click", "type"}:
        draw.ellipse((x-9, y-9, x+9, y+9), outline="#ffbd63", width=2)
    draw.polygon(((x, y), (x, y+14), (x+4, y+10), (x+9, y+10)),
                 fill="#fffaf0", outline="#0f1820")
    return image, labels
