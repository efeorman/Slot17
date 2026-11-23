# Shared app fixtures

The iPhone app (`ios/`) and the Android app (`android/`) must make the same decision for the same
input. These files are that input and the expected answer, and both apps' tests read them:

| File | What it checks |
|---|---|
| `alert-scenarios.json` | Which sound plays, or why none does, for a sequence of matches: the urgent beep, chimes and their gaps, the call tone, Quiet mode, stale data, mute, a refused sound. |
| `server-message.json` | One complete message from the display server, with an unknown field that must be ignored. |
| `ble-frames.json` | Frames of the Bluetooth link (1.4.0) with what they decode to, a changed frame that must be refused, the match keys, and which frames to take in which order. From 1.6.0 also `messages` (a whole state and a file, in pieces, with the JSON they hold) and `operations` (the control frames for an operation's exact JSON text). Made by `p0/bluetooth_frames.py`; `scripts/test_bluetooth_parity.py` keeps it current. |

When a rule changes, change the scenario here first. Then both apps have to follow, or their tests fail.
