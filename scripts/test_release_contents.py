"""What the install tarball holds: scripts/make_release.sh's own `git archive` line, run against HEAD. Found in
review (F36, 24 November 2026): the hardware-free test environment (sim/) went into the tarball, and so into the
Pi image. Needs git and a checkout; skipped without them."""
import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "make_release.sh").read_text(encoding="utf-8")


def archive_args():
    """The script's archive command, as a list, with HEAD for "$ref"."""
    line = next(x for x in SCRIPT.splitlines() if x.startswith("git archive"))
    command = re.split(r"[|>]", line)[0].strip()      # piped to gzip, or written to a file and appended to
    return [a.strip("'\"").replace("$ref", "HEAD") for a in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", command)]


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs git and a checkout")
class ReleaseContents(unittest.TestCase):
    def members(self):
        data = subprocess.run(archive_args(), cwd=ROOT, capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            return [m.name for m in tar if m.isfile()]

    def test_the_tarball_has_the_car_computer_and_not_the_simulator(self):
        names = self.members()
        for wanted in ("slot17/install.sh", "slot17/setup.sh", "slot17/p0/watch.py", "slot17/deploy/setup-car.sh"):
            self.assertIn(wanted, names)
        self.assertEqual([n for n in names if n.startswith("slot17/sim/")], [])

    def test_the_tarball_also_carries_the_page_where_signed_updates_put_it(self):
        """p0/screen.py prefers p0/web/page.html (signed updates bring it there): the tarball has it too, made from
        display/page.html, so a reinstall from a newer tarball never leaves an older copy in front."""
        self.assertIn('git show "$ref:display/page.html" > "$web/slot17/p0/web/page.html"', SCRIPT)
        self.assertIn('tar --append -f dist/slot17.tar -C "$web" slot17/p0/web/page.html', SCRIPT)
        self.assertLess(SCRIPT.index("tar --append"), SCRIPT.index("gzip -9 dist/slot17.tar"))


if __name__ == "__main__":
    unittest.main()
