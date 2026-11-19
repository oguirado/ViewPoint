# SPDX-License-Identifier: AGPL-3.0-only

"""Seeded GUI layouts and painting for the post-freeze holdout profile."""

import random
from dataclasses import dataclass

from PIL import ImageColor

from .scene import State, Widget

PALETTE_ROLES = (
    "main", "detail", "chrome", "menu", "menu_item", "button", "text_field",
    "dialog", "list", "disabled", "outline", "hover", "ink", "light_ink",
)
THEMES = {
    "ocean": (
        "#d7e9ee", "#c6e1e5", "#11344b", "#21495f", "#265b67", "#20536c", "#f3f9fc",
        "#eef6f7", "#b2cdd5", "#566572", "#668b98", "#ec9c42", "#101e2d", "#f6fcfc",
    ),
    "orchard": (
        "#f4e7cc", "#f0dcb6", "#4c3425", "#60402d", "#68432e", "#704529", "#fff7e6",
        "#f8edcf", "#e7c993", "#6c5a50", "#a47749", "#d55a18", "#37231d", "#fff6dc",
    ),
    "night": (
        "#202e34", "#28383f", "#101b24", "#32385e", "#47446f", "#304c66", "#14292e",
        "#252d49", "#273e47", "#48515e", "#70888f", "#e797eb", "#e3ebec", "#f6effe",
    ),
    "violet": (
        "#e8e2f4", "#e9d8ea", "#332450", "#523667", "#5d3c75", "#3e4480", "#fff5fc",
        "#f3e1ed", "#c9b6dd", "#625976", "#9273a9", "#ca4c74", "#2c223b", "#f9f4fc",
    ),
}
DIALOG_RADII = {"centered": 4, "offset": 13, "sheet": 24}
SUBJECTS = ("Coastal", "Forest", "Glacier", "Harbor", "Prairie", "Valley")
SIDEBAR_LABELS = ("Recent", "Tagged", "Shared", "Pinned")


@dataclass(frozen=True)
class HoldoutLayout:
    size: tuple[int, int]
    theme: str
    palette: dict[str, str]
    font_size: int
    header: int
    margin: int
    list_on_right: bool
    list_width_percent: int
    list_top_gap: int
    sidebar_count: int
    notes_height_percent: int
    dialog_style: str
    dialog_width_percent: int
    dialog_height_offset: int
    scrim_percent: int
    subject: str
    note_min_length: int

    @classmethod
    def seeded(cls, rng: random.Random):
        theme = rng.choice(tuple(THEMES))
        lift = rng.randint(-7, 7)
        palette = {
            role: "#" + "".join(f"{max(0, min(255, channel + lift)):02x}"
                                for channel in ImageColor.getrgb(color))
            for role, color in zip(PALETTE_ROLES, THEMES[theme], strict=True)
        }
        return cls(
            size=(rng.randrange(608, 1009, 16), rng.randrange(352, 545, 16)),
            theme=theme, palette=palette, font_size=rng.randint(14, 23),
            header=rng.randint(42, 60), margin=rng.randint(18, 42),
            list_on_right=rng.choice((False, True)),
            list_width_percent=rng.randint(49, 62), list_top_gap=rng.randint(79, 95),
            sidebar_count=rng.randint(0, len(SIDEBAR_LABELS)),
            notes_height_percent=rng.randint(0, 100), dialog_style=rng.choice(tuple(DIALOG_RADII)),
            dialog_width_percent=rng.randint(54, 77), dialog_height_offset=rng.randint(0, 44),
            scrim_percent=rng.randint(38, 73), subject=rng.choice(SUBJECTS),
            note_min_length=rng.randint(12, 27),
        )

    @property
    def dialog_radius(self):
        return DIALOG_RADII[self.dialog_style]

    def appearance(self):
        return {
            "theme": self.theme, "palette": self.palette, "font_size_px": self.font_size,
            "dialog_style": {
                "placement": self.dialog_style, "radius_px": self.dialog_radius,
                "scrim_percent": self.scrim_percent,
            },
        }

    def widgets(self, screen: str, state: State) -> list[Widget]:
        if screen in {"settings", "confirm"}:
            return self.dialog(screen, state)

        width, height = self.size
        margin, header = self.margin, self.header
        content_width = width - 2 * margin
        chrome = [Widget("app_title", "text", (margin, 2, content_width, header-4),
                         f"{self.subject} Notebook")]
        if screen == "detail":
            notes_y = header + 132
            notes_h = 48 + (height - notes_y - 159) * self.notes_height_percent // 100
            button_y = height - 96
            return chrome + [
                Widget("back", "button", (margin, header+9, 112, 37), "Back"),
                Widget("detail_title", "text", (margin, header+49, content_width, 38), state.project),
                Widget("notes_label", "text", (margin, header+91, content_width, 32),
                       f"{self.subject} notes"),
                Widget("notes", "text_field", (margin, notes_y, content_width, notes_h), state.notes),
                Widget("save", "button", (margin, button_y, 145, 40), "Save note",
                       enabled=len(state.notes) >= state.note_min_length),
                Widget("archive", "button", (width-margin-150, button_y, 150, 40), "Archive..."),
                Widget("save_status", "text", (margin, height-49, content_width, 36), state.status),
            ]

        list_w = width * self.list_width_percent // 100
        list_x = width - margin - list_w if self.list_on_right else margin
        side_x = margin if self.list_on_right else list_x + list_w + 20
        side_w = content_width - list_w - 20
        list_y = header + self.list_top_gap
        list_h = height - list_y - 54
        row_w = list_w - 64
        row_y = list_y - state.scroll_offset
        widgets = chrome + [
            Widget("file_menu", "menu", (side_x, header+8, 100, 34), "File"),
            Widget("projects", "text", (list_x, list_y-37, list_w, 33), "Projects"),
            Widget("project_list", "list", (list_x, list_y, list_w, list_h)),
            Widget("project", "button", (list_x+8, row_y+24, row_w, 38), state.project, "project_list"),
            Widget("project_b", "text", (list_x+8, row_y+66, row_w, 30), "Site inventory", "project_list"),
            Widget("project_c", "text", (list_x+8, row_y+101, row_w, 30), "Survey route", "project_list"),
            Widget("scroll_down", "button", (list_x+list_w-48, list_y+list_h-47, 38, 38),
                   "v", "project_list"),
            Widget("active_workspace", "text", (margin, height-46, content_width, 34), state.workspace),
        ]
        if screen == "main_menu":
            widgets.extend((
                Widget("file_popover", "menu", (side_x, header+48, side_w, 88), "Shortcuts", "file_menu"),
                Widget("settings_item", "menu_item", (side_x+7, header+91, side_w-14, 36),
                       "Settings", "file_popover"),
            ))
        else:
            widgets.extend(Widget(f"sidebar_{i}", "text", (side_x, list_y+32*i, side_w, 30), label)
                           for i, label in enumerate(SIDEBAR_LABELS[:self.sidebar_count]))
        return widgets

    def dialog(self, screen: str, state: State) -> list[Widget]:
        width, height = self.size
        w = width * self.dialog_width_percent // 100
        h = (174 if screen == "settings" else 150) + self.dialog_height_offset
        x, y = (width-w)//2, (height-h)//2
        if self.dialog_style == "offset":
            x, y = self.margin, self.header + 11
        elif self.dialog_style == "sheet":
            y = height - h - self.margin
        rx, rw = x + 16, w - 32
        if screen == "settings":
            return [
                Widget("settings_dialog", "dialog", (x, y, w, h), "Workspace settings"),
                Widget("workspace_label", "text", (rx, y+43, rw, 29), "Workspace name", "settings_dialog"),
                Widget("workspace", "text_field", (rx, y+74, rw, 38), state.workspace, "settings_dialog"),
                Widget("apply", "button", (rx+rw-109, y+h-51, 109, 36), "Apply", "settings_dialog"),
            ]
        return [
            Widget("archive_dialog", "dialog", (x, y, w, h), "Archive project?"),
            Widget("archive_message", "text", (rx, y+47, rw, 33), state.project, "archive_dialog"),
            Widget("cancel_archive", "button", (rx, y+h-51, 110, 36), "Cancel", "archive_dialog"),
            Widget("confirm_archive", "button", (rx+rw-136, y+h-51, 136, 36),
                   "Confirm", "archive_dialog"),
        ]

    def paint_widget(self, draw, widget, font, hovered):
        x, y, w, h = widget.slot
        if widget.kind != "text":
            radius = self.dialog_radius if widget.kind == "dialog" else 6
            draw.rounded_rectangle(
                (x, y, x+w-1, y+h-1), radius=radius,
                fill=self.palette[widget.kind if widget.enabled else "disabled"],
                outline=self.palette["hover" if hovered else "outline"], width=3 if hovered else 2,
            )
        if not widget.text:
            return None

        mask, (ox, oy) = font.getmask2(widget.text, mode="L")
        left, top, right, bottom = mask.getbbox()
        text_w, text_h = right-left, bottom-top
        # Dialog and popover titles sit above their authored child controls.
        title = widget.kind == "dialog" or widget.id == "file_popover"
        ink_x, ink_y = x + (16 if title else 8), y + (12 if title else (h-text_h)//2)
        light = widget.id == "app_title" or widget.kind in {"menu", "menu_item", "button"}
        draw.text((ink_x-ox-left, ink_y-oy-top), widget.text, font=font,
                  fill=self.palette["light_ink" if light else "ink"])
        return [ink_x, ink_y, text_w, text_h]
