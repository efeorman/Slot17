# A support package: everything someone needs to help you

When something goes wrong (the detector is degraded, nothing reaches the phone, the connection keeps dropping),
the person helping you needs more than a screenshot. A support package is one zip with all of it. Make it in the
app, or with one command on the car computer.

## From the app (1.5.0)

On the **Pi** screen, card **Support package**:

1. Choose **Quick**, **One drive** or **Everything** (see the choices below). For a drive, pick it from the list:
   the car computer sends its newest drives with their real start times. You can keep only part of it, in
   minutes after the start.
2. **Include this phone's log** is on; **Include drive data with places** is off, and asks before it goes on:
   it adds every place this phone recorded in the last day.
3. **Create the package.** The car computer makes it at the lowest priority and uploads it to your display
   server in pieces (a lost piece is sent again), and the app shows how far it is. Your server adds the phone's
   files to it and keeps the newest three.
4. Then, per package:
   - **Share**: the phone's share sheet, to save it or send it with any app.
   - **Send to team**: to the Slot17 team, after you agree. You get a code such as `S17-7K3Q9X` to tell them in
     the Discord. It goes to the team's inbox at support.slot17.cloud, which nobody can read from the internet,
     takes at most 50 MB and deletes it after 30 days. **Take back** deletes it there sooner.
   - **Delete**: from your server, and from the team's inbox too if you sent it.

The web page's settings list the packages with a **Download** link. It needs car computer software 1.5.0 and
the app 1.8.0 (iPhone) or 1.10.0 (Android). Signed updates bring it to the car computer by themselves.

## On the car computer

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
computer records no places; a package has places only when you add the phone's drive data in the app. What is in it is still yours: drive times, network names and the base stations it
heard tell someone roughly when and where you drove, so send it only to people you trust.

## Safe while the detector runs

It only reads, at the lowest priority, and pauses briefly every 256 KB, so a capture keeps its share. The
manifest records the dropped samples before and after, so the cost is measured, not assumed. "Everything" reads
hundreds of megabytes: do it with the car parked at home.

The system log keeps about a day on a busy car computer, so make the package soon after the drive that went
wrong.
