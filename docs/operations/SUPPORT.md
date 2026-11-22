# A support package: everything someone needs to help you

When something goes wrong (the detector is degraded, nothing reaches the phone, the connection keeps dropping),
the person helping you needs more than a screenshot. One command on the car computer makes one zip with all
of it, which you send them however you like.

```bash
bash ~/Slot17/setup.sh logs
```

It asks what to include, makes `~/slot17-support-<date and time>.zip` and prints where it is. Copy it to your
computer with, on that computer:

```bash
scp <user>@<the car computer's address>:~/slot17-support-*.zip .
```

## The three choices

| Choice | What is in it | Size | When |
|---|---|---|---|
| **Quick check** (recommended) | The report, the setup check, versions, power, USB and network, the settings, the last 3,000 lines of the two newest boots' logs, and the summaries of the last 5 drives | under 1 MB | "Why is it degraded / not connecting / not working?" Usually enough |
| **One drive** | All of that, plus one drive's events (every match and health reading, without the detector's per-candidate lines), its sightings, and every boot's log still on the car computer | 1 to 15 MB | One drive that went wrong |
| **Everything from one drive** | The drive's events with every candidate line and channel table, and on request its recordings (7.5 MB each) | 100 MB and more | Deep analysis, at home |

For a drive you can keep only part of it, in minutes after its start: `--window 30-50`.

Without questions, for scripts or other tools:

```bash
python3 -B ~/Slot17/p0/support.py                       # quick check
python3 -B ~/Slot17/p0/support.py --list                # the drives, with their real start times
python3 -B ~/Slot17/p0/support.py --drive SESSION --window 30-50
python3 -B ~/Slot17/p0/support.py --drive SESSION --everything --recordings --out /tmp/package.zip
```

## What a reviewer finds in it

| File | |
|---|---|
| `REPORT.txt` | First in the zip: the setup check's verdicts, then per drive its start (corrected; folder names can be wrong because the car computer has no clock), length, matches, faults, samples dropped after the start and interference |
| `doctor.json` | `bash setup.sh doctor`, all verdicts |
| `system.txt` | Software version, the computer, power (`get_throttled`, supply voltage), temperature, USB, network and routes, disk, memory, clock, the boots, the services, Bluetooth |
| `settings.json` | Calibration, Bluetooth (on or off and the name), the Pushover switch, whether a display server and signed updates are set up, the service files' start lines |
| `journal/` | The system log per boot, without the detector's per-candidate lines (their number is noted) |
| `drives/<session>/` | `summary.json`, `session-time.json`, `session.json`, and with a drive chosen its `events.jsonl` and sightings |
| `manifest.json` | What is in it and what never is, and the detector's dropped samples before and after the package was made |

## What never goes in

The display server's key, Pushover's tokens, the Bluetooth key, the signing key, Wi-Fi passwords and SSH keys.
Every line also passes a filter that removes anything that looks like a password, token or key. The car
computer records no places. What is in it is still yours: drive times, network names and the base stations it
heard tell someone roughly when and where you drove, so send it only to people you trust.

## Safe while the detector runs

It only reads, at the lowest priority, and pauses briefly every 256 KB, so a capture keeps its share. The
manifest records the dropped samples before and after, so the cost is measured, not assumed. "Everything" reads
hundreds of megabytes: do it with the car parked at home.

The system log keeps about a day on a busy car computer, so make the package soon after the drive that went
wrong.

**Coming next:** the same package from the app (a Support package card on the Pi screen, with Share), and a way
to send it to the Slot17 team.
