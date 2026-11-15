"""One declared source boundary for root scanners; component suites are mandatory."""
import json
from pathlib import Path, PurePosixPath


def root_owned_paths(root, names):
    """Filter canonical relative names, refusing undeclared component trees.

    Small standalone legacy fixtures without components need no manifest. A
    component universe always requires an explicit, valid ownership document.
    The boundary is lexical: callers retain their existing symlink checks.
    """
    names = list(names)
    manifest = Path(root) / 'OWNERSHIP.json'
    components = {}
    if manifest.exists() or manifest.is_symlink():
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError('ownership manifest must be a regular file')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('schema') != 'viewpoint.ownership.v1'
                or data.get('root') != 'ViewPoint'
                or not isinstance(data.get('components'), dict)):
            raise ValueError('invalid ownership manifest')
        components = data['components']
        for prefix, owner in components.items():
            if (not isinstance(owner, str) or not owner
                    or prefix != 'components/' + owner + '/'):
                raise ValueError('ownership may delegate only a named component root')
    owned = []
    for name in names:
        if not name:
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or str(path) != name or '..' in path.parts or '\\' in name:
            raise ValueError('non-canonical ownership path')
        if path.parts[0] == 'components':
            if not any(name.startswith(prefix) for prefix in components):
                raise ValueError('undeclared component path: ' + name)
        else:
            owned.append(name)
    return owned
