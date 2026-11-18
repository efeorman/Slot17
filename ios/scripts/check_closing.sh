#!/bin/bash
# What happens when the app is closed, or started by iOS with nobody opening it. Real background launches
# and swipes cannot be caused from a test, so debug switches stand in for them; the code run is the real code.
device="$1"
out="${2:-build/check}"
log="$(xcrun simctl get_app_container "$device" nl.slot17.app data)/Documents/slot17.log"
failed=0
check() {
  if bash -c "$2"; then echo "ok   $1"; else echo "FAIL $1"; failed=1; fi
}
report="$out/closing-check.txt"
mkdir -p "$out" && : > "$report" || { echo "FAIL cannot write $report"; exit 1; }
run() {
  # 1. A normal open, then the process dies with no warning, leaving its tile behind.
  xcrun simctl launch "$device" nl.slot17.app --fresh --demo --no-audio > /dev/null
  sleep 8
  check "a normal open shows the tile" "grep -qE 'live activity (started|taken over)' '$log'"
  check "a normal open starts the sound after the open" "awk '/app opened/{o=1} /audio skipped/{print o+0; exit}' '$log' | grep -q 1"
  xcrun simctl terminate "$device" nl.slot17.app
  sleep 1

  # 2. iOS starts the app by itself.
  before=$(wc -l < "$log")
  xcrun simctl launch "$device" nl.slot17.app --demo --no-audio --simulate-background-launch > /dev/null
  sleep 8
  part="$out/closing-background.log"
  tail -n +$((before + 1)) "$log" > "$part"
  check "that run really started" "grep -q 'app started' '$part'"
  check "started by iOS: it says it stays silent" "grep -q 'staying silent' '$part'"
  check "started by iOS: the sound stays off" "! grep -qE 'audio (armed|skipped)' '$part'"
  check "started by iOS: location stays off" "! grep -q 'location recording started' '$part'"
  check "started by iOS: the tile left behind is ended" "grep -q 'live activity ended: the app was not opened' '$part'"
  check "started by iOS: iOS says no tile is showing" "grep -q 'live activity end finished; 0 still showing' '$part'"
  xcrun simctl terminate "$device" nl.slot17.app
  sleep 1

  # 3. The app is told it is being closed.
  before=$(wc -l < "$log")
  xcrun simctl launch "$device" nl.slot17.app --demo --no-audio --simulate-close-after 6 > /dev/null
  sleep 10
  part="$out/closing-told.log"
  tail -n +$((before + 1)) "$log" > "$part"
  check "closing: a tile was running first" "grep -qE 'live activity (started|taken over)' '$part'"
  check "closing: the tile is ended" "grep -q 'live activity ended: the app was closed' '$part'"
  check "closing: iOS says no tile is showing" "grep -q 'live activity end finished; 0 still showing' '$part'"
  xcrun simctl terminate "$device" nl.slot17.app
  sleep 1

  # 4. Opened in the car with a server that cannot be reached: it must say so once.
  before=$(wc -l < "$log")
  xcrun simctl launch "$device" nl.slot17.app --no-audio --pretend-car-audio --link "http://192.0.2.1:9/?key=abcdefgh12345" > /dev/null
  sleep 75
  part="$out/closing-never-connected.log"
  tail -n +$((before + 1)) "$log" > "$part"
  check "never connected: the warning plays once" "[ \"\$(grep -c 'never connected since the app was opened' '$part')\" = 1 ]"
  xcrun simctl terminate "$device" nl.slot17.app
  sleep 1

  # 5. Going to sleep stops everything that uses power, and nothing starts it again by itself.
  before=$(wc -l < "$log")
  xcrun simctl launch "$device" nl.slot17.app --no-audio --force-sleep-after 6 > /dev/null
  sleep 20
  part="$out/closing-sleep.log"
  tail -n +$((before + 1)) "$log" > "$part"
  check "sleep: the sound stops" "grep -q 'audio disarmed' '$part'"
  check "sleep: location stops" "grep -q 'location recording stopped' '$part'"
  check "sleep: nothing starts again by itself" "! sed '1,/going to sleep/d' '$part' | grep -qE 'location recording started|audio skipped|stream ended'"
  xcrun simctl terminate "$device" nl.slot17.app
}
run > "$report" 2>&1
cat "$report"
if grep -q '^FAIL' "$report"; then exit 1; fi
# Every check must have run, or the script itself went wrong.
[ "$(grep -c '^ok ' "$report")" -eq 15 ] || { echo "FAIL not all 15 checks ran"; exit 1; }
exit $failed
