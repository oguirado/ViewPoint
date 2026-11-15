# HB-VC runner for R-s6-z1-size (tracked; JDOCS-2)
import hashlib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from visioncore import pack
from visioncore.params import PARAMS
t = pack.z1_text()
b = t.encode() if isinstance(t, str) else bytes(t)
print(json.dumps({"z1_bytes": len(b), "z1_max_b": PARAMS["Z1_MAX_B"], "z1_sha256": hashlib.sha256(b).hexdigest(),
                  "z1_version": getattr(pack, "Z1_VERSION", "z1.1")}, sort_keys=True))
