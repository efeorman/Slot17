"""Keep tests away from the real Pushover switch (~/.config/slot17-notify.json).

The car computer runs these tests at its starts. If the operator has switched Pushover off in the app,
tests that expect messages to be queued would fail there and nowhere else. Importing this module points
`notify.SWITCH_PATH` at a file in a fresh, empty temporary folder, so the switch reads "on" in every
test unless a test writes its own.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import notify  # noqa: E402

notify.SWITCH_PATH = str(Path(tempfile.mkdtemp(prefix="slot17-switch-")) / "slot17-notify.json")
