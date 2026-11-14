#!/usr/bin/env python3
"""Small GTK editor with descriptor-relative, no-symlink exchange access."""
import os
from pathlib import Path
import stat
import sys

EXCHANGE = Path('/profile/exchange')
MAX_BYTES = 1024 * 1024


def check_name(name):
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\x00' in name or '..' in name or len(name.encode('utf-8')) > 255):
        raise ValueError('invalid file name')
    return name


def open_document(name, root, write=False):
    check_name(name)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= (os.O_WRONLY | os.O_CREAT) if write else os.O_RDONLY
        fd = os.open(name, flags, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('file must be regular and within size limit')
    return fd


def read_document(name, root=EXCHANGE):
    with os.fdopen(open_document(name, root), 'rb') as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('file exceeds size limit')
    return data.decode('utf-8')


def write_document(name, text, root=EXCHANGE):
    data = text.encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('file exceeds size limit')
    with os.fdopen(open_document(name, root, write=True), 'wb') as target:
        target.truncate(0)
        target.write(data)


def main():
    # Delayed import keeps the file policy independently testable without GTK.
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gdk, GLib, Gtk

    if len(sys.argv) != 3 or sys.argv[1] != '--tile':
        return 2
    GLib.set_prgname('srf-notepad')
    window = Gtk.Window(title='srf-notepad - untitled')
    window.set_wmclass('srf-notepad', 'srf-notepad')
    window.set_default_size(400, 300)
    window.connect('destroy', Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    window.add(box)
    toolbar = Gtk.Box(spacing=4)
    box.pack_start(toolbar, False, False, 0)
    name = Gtk.Entry()
    name.set_placeholder_text('File name')
    name.get_accessible().set_name('File name')
    toolbar.pack_start(name, True, True, 0)
    editor = Gtk.TextView()
    editor.get_accessible().set_name('Document')
    scroll = Gtk.ScrolledWindow()
    scroll.add(editor)
    box.pack_start(scroll, True, True, 0)
    status = Gtk.Label(label='')
    box.pack_start(status, False, False, 0)
    buffer = editor.get_buffer()

    def action(kind):
        try:
            if kind == 'New':
                buffer.set_text('')
                name.set_text('')
                status.set_text('new document')
                window.set_title('srf-notepad - untitled')
                return
            filename = check_name(name.get_text())
            if kind == 'Open':
                buffer.set_text(read_document(filename))
                status.set_text('opened ' + filename)
            else:
                write_document(filename, buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
                status.set_text('saved ' + filename)
            window.set_title('srf-notepad - ' + filename)
        except (OSError, ValueError, UnicodeError):
            status.set_text('REFUSED: invalid file or exchange access')

    for label in ('Open', 'Save', 'New'):
        button = Gtk.Button(label=label)
        button.get_accessible().set_name(label)
        button.connect('clicked', lambda unused, kind=label: action(kind))
        toolbar.pack_start(button, False, False, 0)

    def keypress(unused, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            key = Gdk.keyval_name(event.keyval).lower()
            if key in ('s', 'o'):
                action('Save' if key == 's' else 'Open')
                return True
        return False

    window.connect('key-press-event', keypress)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ImportError, OSError):
        print('AUSENTE — GTK runtime unavailable', file=sys.stderr)
        sys.exit(4)
