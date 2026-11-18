"""Release and source/configuration provenance, computed once per process."""
from functools import lru_cache
import hashlib
from pathlib import Path
import platform

RELEASE = "p0-2.0-rc3"
# The software version people see (VERSION at the repository root; a test keeps the two equal). Signed
# updates replace p0/ but not VERSION, so this is what the car computer reports to the app.
SOFTWARE = "1.2.9"


@lru_cache(maxsize=1)
def provenance():
    import numpy
    import scipy
    root = Path(__file__).resolve().parent
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))}
    digest = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in files.items()).encode()).hexdigest()
    config = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
              (root / "calibration.json", root / "downlink_channels.json",
               root / "calibration.local.json", root / "downlink_channels.local.json") if p.is_file()}
    return {"release": RELEASE, "source_sha256": digest, "source_files": files,
            "config_sha256": config, "python": platform.python_version(),
            "numpy": numpy.__version__, "scipy": scipy.__version__}
