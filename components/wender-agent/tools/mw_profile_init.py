# @module {"id":"wa.tools.mw_profile_init","layer":4,"owner":"LEARN","deps":["wa.learn.mw_profiles","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""Stage private inactive profiles, then explicitly activate an operator token.

The profile directory must already exist with mode 0700 [TARGET]. Identity and
capsule defaults to --container; identity-env defaults to the installed surface mapping.
Stage first, create the acknowledgment using the printed operator procedure, then
run mw_profile_init --site <site> --activate to pin that existing token.
Standalone use requires installed producers and supported workflow contracts. No token is ever created,
replaced or chmodded.
"""
import argparse
from dataclasses import replace
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import shlex
import sys
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import memoryweb_gate as gate
from wender.learn import mw_profiles as profiles
from wender.perceive.srf_identity import IDENTITY_ENV


# @anchor:wa.s4.mw_profile_init._Parser
class _Parser(argparse.ArgumentParser):
    # @anchor:wa.s4.mw_profile_init._Parser.error
    def error(self, message):
        raise profiles.ProfileError(2) from None


# @anchor:wa.s4.mw_profile_init.main
def main(argv=None, *, profile_dir=None):
    parser = _Parser(add_help=False, description=__doc__)
    parser.add_argument('--site', required=True)
    for name in ('container', 'window', 'export-dir', 'capsule', 'identity-env'):
        parser.add_argument('--' + name)
    parser.add_argument('--activate', action='store_true')
    temporary = None
    try:
        args = parser.parse_args(argv)
        profiles._need(re.fullmatch(r'[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?', args.site) is not None,
                       'init_site', 2)
        directory = Path.home() / '.config/memoryweb/profiles' if profile_dir is None else Path(profile_dir)
        with profiles._guard(directory, directory=True) as parent:
            profiles._need(stat.S_IMODE(os.fstat(parent).st_mode) == 0o700, 'init_directory')
            fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            name = args.site + '.json'
            if args.activate:
                profiles._need(all(getattr(args, k) is None for k in
                    ('container', 'window', 'export_dir', 'capsule', 'identity_env')), 'init_activation_mapping', 2)
                profile = profiles.load_profile(args.site, profile_dir=directory)
                profiles._need(profile.export_dir is not None and profile.ack_sha256 is None, 'init_inactive')
                with profiles._guard(directory / name) as fd:
                    original = profiles._metadata(os.fstat(fd))
                    value = gate.strict_load(profiles._read(fd, 16384))
                with profiles._guard(profile.live_ack_source) as fd:
                    raw = profiles._read(fd, 4096)
                    profiles._need(32 <= len(raw) <= 4096, 'init_token_size')
                    digest = hashlib.sha256(raw).hexdigest()
                    del raw
                    # Verify the token stays guarded until the profile is committed.
                    value['ack_sha256'] = digest
                    temporary = '.activate-' + uuid.uuid4().hex
                    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600, dir_fd=parent)
                    try:
                        os.fchmod(out, 0o600)  # [TARGET] private profile, independent of umask
                        _write(out, value)
                        profiles._need(profiles._metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) == original,
                                       'init_profile_binding')
                        profiles._token_digest(replace(profile, ack_sha256=digest))
                        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                        temporary = None
                        os.fsync(parent)
                    finally:
                        os.close(out)
                        if temporary is not None:
                            os.unlink(temporary, dir_fd=parent)
                            temporary = None
                print('Profile activated. Standalone use requires installed producers and supported workflow contracts.')
            else:
                profiles._need(all(getattr(args, key) is not None for key in
                    ('container', 'window', 'export_dir')), 'init_mapping', 2)
                args.capsule = args.container if args.capsule is None else args.capsule
                args.identity_env = IDENTITY_ENV if args.identity_env is None else args.identity_env
                coordinates = {k: profiles._text(getattr(args, k), 256, True)
                               for k in ('capsule', 'container', 'window')}
                identity = profiles._path(args.identity_env)
                export = profiles._path(args.export_dir)
                with profiles._guard(identity), profiles._guard(export, directory=True):
                    value = dict(coordinates, identity_env=str(identity), export_dir=str(export),
                        announce_gate='trusted_notice_v1', allowed_modes=['replay', 'shadow'],
                        live_ack_source={'kind': 'file', 'path': str(directory / (args.site + '.ack'))},
                        ack_sha256=None)
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600, dir_fd=parent)
                    try:
                        os.fchmod(out, 0o600)  # [TARGET] private profile, independent of umask
                        _write(out, value)
                        os.fsync(parent)
                    except BaseException:
                        os.unlink(name, dir_fd=parent)
                        raise
                    finally:
                        os.close(out)
                token_path = shlex.quote(str(directory / (args.site + '.ack')))
                print('Inactive profile created. Standalone use requires installed producers and supported workflow contracts.')
                print(f'head -c 32 /dev/urandom > {token_path}; chmod 600 {token_path}')
                print('Activation pins the existing operator-created token; the initializer never changes it.')
                print('python3 tools/mw_profile_init.py --site ' + shlex.quote(args.site) + ' --activate')
        return 0
    except profiles.ProfileError as exc:
        print(exc.category)
        return exc.rc
    except (OSError, ValueError, TypeError):
        print('OUT_OF_SUPPORT')
        return 5


# @anchor:wa.s4.mw_profile_init._write
def _write(fd, value):
    raw = (gate.canon(value) + '\n').encode('utf-8')
    while raw:
        count = os.write(fd, raw)
        profiles._need(count > 0, 'init_write')
        raw = raw[count:]
    os.fsync(fd)


if __name__ == '__main__':
    sys.exit(main())
