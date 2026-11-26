# Display server

The display sits next to the detector, not inside it. The detector works without it.

```
Pi: detector --heartbeat, events--> publisher --outbound HTTP--> server.py :5732 --live stream--> app or web page
```

The publisher can only be started while the detector runs. A stop from the app or web page ends both; if the detector stops by itself, the publisher stays, to report the fault and answer the app's requests. Nothing connects to the Pi, so a command can only travel back on a request the Pi already makes.

## How fresh is the data?

The detector starts a new window every 0.25 seconds, each also looking 64 ms back so no burst is cut, and the publisher checks for new events every 0.1 seconds when it isn't busy sending. Each burst carries its age, and that age is estimated conservatively across the Pi, server and phone without comparing wall clocks:

1. Before each push, the Pi gets a one-use `/api/clock` ticket from the server. The ticket expires after 6 seconds, and it brackets how long the upload took.
2. The phone brackets its own clock offset to the server with a timed `/api/state` request.
3. Slow or unavailable timing suppresses a sound rather than playing one late.

These are software limits, not a calibrated measurement of when the radio transmitted.

## Sounds

| Sound | When | Limits |
|---|---|---|
| Chime | A new match at or above `red_db`, whose burst is at most `max_age_s` old | At most one per `chime_gap_channel_s` per channel and one per `chime_gap_any_s` overall |
| Urgent beep | A new match at or above `urgent_db` | `urgent_gap_s` between beeps; a beep inside the gap is dropped, not queued |
| Call tone | A new match in a call pattern, from `call_db` up to the chime level; soft and lower than the chime | `call_gap_channel_s` per channel, `call_gap_any_s` between call tones; never holds back a chime or urgent beep. App 1.4.8 and the web page; `call_db` 99 switches it off |
| Link warning | Nothing from the server for `link_lost_after_s` | App only |
| Receiver warning | `receiver_quiet_windows` quiet windows in a row | Repeats every `receiver_tone_repeat_s` |

Levels are in dB above that channel's own noise floor. They're signal strength, not distance.

## API

| Endpoint | Key | What it does |
|---|---|---|
| `GET /healthz` | None | Answers 200 if the server is running |
| `GET /` | Viewer | The web page. `/?key=VIEWER_KEY` sets the cookie |
| `GET /api/state` | Viewer | The latest state |
| `GET /api/stream` | Viewer | The live stream |
| `POST /api/command` | Viewer | Stop the detector |
| `POST /api/op` | Viewer | `mark` (a sighting), `logs` (ask the Pi for its log), `poweroff`, `notify` (the Pushover switch), `doctor` (the setup check), `bluetooth` and `support` (a support package); each has a 32-hex id, so a repeat is harmless. A client offers only what the car computer lists in its `features` |
| `POST /api/app-log`, `POST /api/pi-log` | Viewer, Pi | Store a log of up to 1 MB. The server chooses the name, and keeps at most 200 files and 100 MB of each kind |
| `GET /api/logs`, `GET /api/logs/NAME` | Viewer | List and read stored logs |
| `GET /api/config` | Viewer | `{"version": ..., "config": ...}` from `app-config.json` |
| `GET /api/sounds/NAME.wav` | Viewer | A WAV from `sounds/`; lowercase name, at most 2 MB |
| `GET /api/clock`, `POST /api/push` | Pi | The publisher's ticket and upload |
| `GET /api/pi-on?token=KEY` | Status | Plain `on` or `off`, for a phone shortcut |

`/api/pi-on` means the server accepted a push within the last 30 seconds. It can lag a shutdown by that long, and says `off` after a server restart. Its key, `pi_status_key`, is created by the setup and opens nothing else; `bash ~/Slot17/setup.sh server --show` prints the full link.

## Changing the app without an app update

The app reads `app-config.json` from the server when it connects, and every 5 minutes. **Settings > Check for new settings** checks at once.

1. Edit `/opt/slot17-display/app-config.json` on the server, for example with `sudo nano /opt/slot17-display/app-config.json`. The server picks up the change without a restart, and updates never overwrite it.
2. For a recorded sound, copy the WAV into the server's `sounds` folder, then set for example `"chime": {"wav": "my_bell.wav"}`:
   ```bash
   sudo install -o slot17 -g slot17 -m 0644 my_bell.wav /opt/slot17-display/sounds/
   ```

**Safety rules:**

- A file with any out-of-range value is refused as a whole, and the app keeps what it had. Settings shows why.
- Anything you changed by hand in the app stays yours. **Use server settings** clears that.
- Quiet mode never silences an urgent beep.

| Section | Keys |
|---|---|
| `message` | Text for a banner in the app; `""` shows none |
| `rules` | `red_db`, `yellow_db`, `urgent_db`, `urgent_gap_s`, `chime_gap_any_s`, `chime_gap_channel_s`, `max_age_s`, `quiet_red_db`, `quiet_gap_s`, `call_db`, `call_gap_channel_s`, `call_gap_any_s` |
| `warnings` | `link_lost_after_s`, `receiver_quiet_windows`, `receiver_tone_repeat_s` |
| `marks` | `summary_window_s`: how long after a sound a mark counts for it |
| `location` | `track_every_s` |
| `display` | `weak_colour`, `silent_colour`, `chime_colour`, `strong_outline_colour`, `strong_badge`, `frequent_text`, `pulse_on_new_strong`, `tile_gap_s`, `floor_quiet_db` (the noise floor with the antenna hearing nothing, measured once at home) |
| `sounds` | `urgent`, `chime`, `call`, `warning`, `link_lost`: a recipe or `{"wav": "name.wav"}`. Add `call` only once every phone has app 1.4.8: older apps refuse a file that names a sound they do not know |

**A sound recipe:**

```json
"chime": {"wave": "triangle", "peak": 0.65, "attack_s": 0.015, "release_s": 0.25,
          "notes": [[988, 0, 0.45], [1319, 0.17, 0.45]]}
```

- **`wave`:** `sine`, `square`, `triangle` or `bell`. A bell also takes `fade_s`.
- **Each note:** `[hz, start_s, length_s]`, with 50–10,000 Hz, starting within 3 seconds and at most 3 seconds long.
- **Total:** at most 16 notes and 4 seconds.

**Colours mean what the app does, never distance or vehicle type:**

| Colour | Meaning |
|---|---|
| Grey-blue | Heard, weak |
| Yellow | Heard, silent |
| Red | Chime level |
| Red with a white outline and badge | Urgent level |

Each colour must be `#RRGGBB`, contrast at least 3 to 1 with the background, and not be green, because green reads as safe.

"Frequent bursts" means the detector counted many bursts close together. It isn't decoded traffic, so it doesn't mean speech.

## Connection details

From release 9 the car computer's push carries a `link` block, read every few seconds without running programs: `via` and `networks` (`cable`, `4G stick`, `Wi-Fi`, `none`, else `other`), `device`, the last minute's `pushes`, `failed`, `failovers`, `median_ms` and `slowest_ms`, `wifi_dbm` from `/proc/net/wireless` when the hotspot is in use, `clock_synced`, and `stick` from the stick check's `/run/slot17-modem.json` when that is under two minutes old: `connected`, `network`, `provider`, `roaming`, `bars`, `rsrp`, `rsrq`, `snr`, `cell`, `ping_ms`, `ping_worst_ms`, `ping_lost`, `down_s`. The server rebuilds it from these fields only, within fixed ranges, and drops a malformed block without refusing the push. App 1.4.9 shows it under the chain on the Pi screen's Connection card, with the phone's own last round trip to the server.

## Limits on the server

| Limit | Value |
|---|---|
| Connections at once | 32 in total, 12 from one address |
| Phone streams at once | 8 |
| Request body | 64 KB, or 1 MB for a log |
| Time for headers or a body | 10 seconds each, or 45 seconds for a log |
| Idle connection | 15 seconds |

A connection without a valid key closes after one answer.

## Web page sound on a phone

Browsers need one tap on **Start** before they may play sound. On iOS the page also loops a near-silent `<audio>` element, because the ring switch otherwise mutes Web Audio while the page looks healthy. Locking the phone suspends the audio until the next tap, so use the app if you need sound with the screen locked.
