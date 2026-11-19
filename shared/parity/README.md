# Shared app fixtures

The iPhone app (`ios/`) and the Android app (`android/`) must make the same decision for the same
input. These files are that input and the expected answer, and both apps' tests read them:

| File | What it checks |
|---|---|
| `alert-scenarios.json` | Which sound plays, or why none does, for a sequence of matches: the urgent beep, chimes and their gaps, the call tone, Quiet mode, stale data, mute, a refused sound. |
| `server-message.json` | One complete message from the display server, with an unknown field that must be ignored. |

When a rule changes, change the scenario here first. Then both apps have to follow, or their tests fail.
