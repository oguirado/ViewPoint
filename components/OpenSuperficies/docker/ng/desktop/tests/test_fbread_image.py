#!/usr/bin/env python3
"""Check the desktop recipe and static ELF without running a container or X."""
import hashlib
from pathlib import Path
import struct
import sys

def main():
    root = Path(__file__).resolve().parents[4]
    recipe = (root / 'docker/ng/desktop/Dockerfile').read_text()
    binary = (root / 'build/room_RB2/srf-fbread').read_bytes()
    digest = hashlib.sha256(binary).hexdigest()
    copy = 'COPY --chown=0:0 --chmod=0555 build/room_RB2/srf-fbread /usr/local/libexec/srf-fbread'


    def valid(text):
        return copy in text and f'{digest}  /usr/local/libexec/srf-fbread' in text and 'sha256sum -c -' in text


    if not valid(recipe):
        print('fbread recipe missing mode, ownership, COPY or SHA pin', file=sys.stderr)
        sys.exit(5)
    if valid(recipe.replace(digest, '0' * 64)) or valid(recipe.replace('--chmod=0555', '--chmod=0755')):
        print('recipe negative control admitted', file=sys.stderr)
        sys.exit(5)
    # ELF64 LE, AArch64; no dynamic loader (PT_INTERP) or dynamic section (PT_DYNAMIC).
    if binary[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', binary, 18)[0] != 183:
        print('fbread must be an AArch64 ELF64 binary', file=sys.stderr)
        sys.exit(5)
    phoff = struct.unpack_from('<Q', binary, 32)[0]
    phsize, phnum = struct.unpack_from('<HH', binary, 54)
    for i in range(phnum):
        if struct.unpack_from('<I', binary, phoff + i * phsize)[0] in (2, 3):
            print('fbread must be statically linked', file=sys.stderr)
            sys.exit(5)
    print(f'RB2 static binary and image pin PASS sha256={digest}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, struct.error) as exc:
        print(f'fbread image check failed: {type(exc).__name__}', file=sys.stderr)
        sys.exit(5)
