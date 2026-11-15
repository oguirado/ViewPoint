#!/usr/bin/env python3
"""Post-build image inspection. Operator only; not part of offline checks."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    recipe = json.loads((root / 'docker/program/recipe.json').read_text())
    image = recipe['image']
    try:
        metadata = json.loads(subprocess.run(['docker', 'image', 'inspect', image],
                              check=True, capture_output=True, text=True, timeout=15).stdout)[0]
        config = metadata['Config']
        if (metadata['Architecture'] != 'arm64' or config['User'] != '10001:0'
                or config['Entrypoint'] != ['/usr/local/bin/identity-entrypoint.sh']):
            raise ValueError()
        # No display is launched: override the entrypoint for every probe.
        probe = ('import os; '
                 'assert os.getuid() == 10001; '
                 'assert os.access("/usr/local/bin/identity-entrypoint.sh", os.X_OK); '
                 'assert os.access("/usr/local/bin/identity-xpra-layer.sh", os.X_OK); '
                 'assert os.access("/usr/local/libexec/srf-notepad.py", os.X_OK)')
        subprocess.run(['docker', 'run', '--rm', '--network=none', '--read-only',
                        '--cap-drop=ALL', '--entrypoint', 'python3', image, '-I', '-c', probe],
                       check=True, capture_output=True, text=True, timeout=15)
        # Package queries only; never the graphical entrypoint.
        for package, version in recipe['package_pins'].items():
            result = subprocess.run(['docker', 'run', '--rm', '--network=none', '--read-only',
                                     '--cap-drop=ALL', '--entrypoint', 'dpkg-query', image,
                                     '-W', '-f=${Version}', package], check=True,
                                    capture_output=True, text=True, timeout=15)
            if result.stdout != version:
                raise ValueError()
        print('program image contract passed')
        return 0
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        print('REFUSED: program_image_contract', file=sys.stderr)
        return 6


if __name__ == '__main__':
    sys.exit(main())
