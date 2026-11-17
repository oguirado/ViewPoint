"""Execute approved Python in this service process and record its own RSS.

No child creation, waiting, or termination is implemented here. The service
manager owns all descendants and the authoritative exit result.
"""
import json
import os
from pathlib import Path
import resource
import runpy
import sys
import tempfile

if __name__ == '__main__':
    output = Path(sys.argv[1])
    sys.argv = sys.argv[2:]
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    finally:
        record = {'peak_rss_bytes': max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                       resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)*1024}
        fd, path = tempfile.mkstemp(prefix='.rss-', dir=output.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(json.dumps(record, sort_keys=True, separators=(',', ':'))+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, output)
        finally:
            if os.path.exists(path):
                os.unlink(path)
