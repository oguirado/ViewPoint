# SPDX-License-Identifier: AGPL-3.0-only
"""Private native OCR worker. The supervisor supplies the deadline and thread cap."""
import ctypes as ct
import json
from pathlib import Path
import struct
import sys
import unicodedata

import numpy as np

from source.proc import require_namespace


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


class Tesseract:
    def __init__(self, directory):
        self.api = None
        self.reason = "tessdata_absent"
        try:
            lib = ct.CDLL("libtesseract.so.5")
            signatures = {
                "Create": (ct.c_void_p, ()),
                "Delete": (None, (ct.c_void_p,)),
                "Init3": (ct.c_int, (ct.c_void_p, ct.c_char_p, ct.c_char_p)),
                "SetPageSegMode": (None, (ct.c_void_p, ct.c_int)),
                "SetImage": (None, (ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int)),
                "GetUTF8Text": (ct.c_void_p, (ct.c_void_p,)),
                "Clear": (None, (ct.c_void_p,)),
            }
            for name, (returns, arguments) in signatures.items():
                function = getattr(lib, "TessBaseAPI" + name)
                function.restype, function.argtypes = returns, arguments
            lib.TessDeleteText.argtypes = (ct.c_void_p,)
            lib.TessDeleteText.restype = None
        except (OSError, AttributeError):
            self.reason = "tesseract_unavailable"
            return
        api = lib.TessBaseAPICreate()
        if not api or lib.TessBaseAPIInit3(api, bytes(directory), b"eng"):
            if api:
                lib.TessBaseAPIDelete(api)
            self.reason = "tesseract_initialization_failed"
            return
        self.lib, self.api = lib, api
        self.reason = None

    def close(self):
        if self.api:
            self.lib.TessBaseAPIDelete(self.api)
            self.api = None

    def read(self, image):
        lib, api = self.lib, self.api
        lib.TessBaseAPISetPageSegMode(api, 7)
        lib.TessBaseAPISetImage(api, image.ctypes.data, image.shape[1], image.shape[0], 1, image.strides[0])
        pointer = lib.TessBaseAPIGetUTF8Text(api)
        try:
            result = normalize(ct.string_at(pointer).decode("utf-8")) if pointer else ""
        finally:
            if pointer:
                lib.TessDeleteText(pointer)
            lib.TessBaseAPIClear(api)
        return [result, None] if result else [None, "ocr_no_text"]


def send(stdout, value):
    data = json.dumps(value, ensure_ascii=False).encode()
    stdout.write(struct.pack("!I", len(data)) + data)
    stdout.flush()


def main():
    require_namespace()
    engine = Tesseract(Path(sys.argv[1]))
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        send(stdout, engine.reason)
        if engine.reason:
            return
        while header := stdin.read(8):
            if len(header) != 8:
                return
            w, h = struct.unpack("!II", header)
            if not w or not h or w*h > 4 << 20:
                return
            data = stdin.read(w*h)
            if len(data) != w*h:
                return
            image = np.frombuffer(data, dtype=np.uint8).reshape(h, w)
            send(stdout, engine.read(image))
    finally:
        engine.close()


if __name__ == "__main__":
    main()
