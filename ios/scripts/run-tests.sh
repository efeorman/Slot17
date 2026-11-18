#!/bin/bash
# Unit tests, then the UI tests against the pretend car, then checks of what really happened.
# None of this can show anything about CarPlay, a locked phone or a real iPhone's rules.
set -uo pipefail
out=build/check
shots="$PWD/$out/screens"
mkdir -p "$shots"
device=$(xcrun simctl list devices available -j | python3 -c '
import json, sys
for runtime, devices in json.load(sys.stdin)["devices"].items():
    if "iOS" in runtime:
        for d in devices:
            if d["name"].startswith("iPhone"):
                print(d["udid"]); raise SystemExit
raise SystemExit("no iPhone simulator found")')
xcrun simctl boot "$device" || true
xcrun simctl bootstatus "$device" -b >/dev/null

status=0
xcodebuild build-for-testing -project Slot17.xcodeproj -scheme Slot17 -destination "id=$device" \
  -derivedDataPath build/test > "$out/build-for-testing.log" 2>&1 \
  || { grep -E "error:" "$out/build-for-testing.log" | head -40; exit 1; }

# The app started directly, with everything saved that explains a screen that never appears.
app=build/test/Build/Products/Debug-iphonesimulator/Slot17.app
xcrun simctl install "$device" "$app"
# Location: granted up front, at a fixed place, so no permission prompt interrupts the tests.
xcrun simctl privacy "$device" grant location nl.slot17.app || true
xcrun simctl location "$device" set 52.3702,4.8952 || true
# NO_AUDIO=1: a Mac with no audio system, where starting the sound engine hangs the app.
export TEST_RUNNER_NO_AUDIO="${NO_AUDIO:-}"
if [ -n "${NO_AUDIO:-}" ]; then xcrun simctl spawn "$device" launchctl setenv SLOT17_NO_AUDIO 1; fi
xcrun simctl launch "$device" nl.slot17.app --fresh --demo --screenshot-values ${NO_AUDIO:+--no-audio} || status=1
sleep 15
xcrun simctl io "$device" screenshot "$out/launch-check.png" || true
pid=$(pgrep -x Slot17 | head -1 || true)
echo "app process: ${pid:-none}" | tee "$out/launch-check.txt"
[ -n "$pid" ] || { echo "the app is not running 15 s after launch"; status=1; }
if [ -n "$pid" ]; then
  ps -o pid,pcpu,rss,etime,stat -p "$pid" | tee -a "$out/launch-check.txt"
  sample "$pid" 3 -file "$out/launch-sample.txt" >/dev/null 2>&1 || true
fi
xcrun simctl spawn "$device" log show --last 1m --style compact --predicate 'process == "Slot17"' > "$out/launch-system-log.txt" 2>&1 || true
cp ~/Library/Logs/DiagnosticReports/Slot17* "$out/" 2>/dev/null || true
xcrun simctl terminate "$device" nl.slot17.app || true

bash scripts/check_closing.sh "$device" "$out" || status=1

python3 scripts/e2e_harness.py "$PWD/$out/e2e" > "$out/harness.log" 2>&1 &
harness=$!
trap 'kill $harness 2>/dev/null' EXIT
sleep 2

summary='error:|Test Case .*failed|Executed [0-9]+ tests|\*\* TEST'

# Unit tests and the drive first: the screenshot tests start fresh, which wipes the app's log.
TEST_RUNNER_SHOT_DIR="$shots" xcodebuild test-without-building -project Slot17.xcodeproj -scheme Slot17 \
  -destination "id=$device" -derivedDataPath build/test -resultBundlePath "$out/results.xcresult" \
  -only-testing:Slot17Tests -only-testing:Slot17UITests/EndToEndTests -parallel-testing-enabled NO > "$out/test.log" 2>&1 || status=1
grep -E "$summary" "$out/test.log" | tail -40

container=$(xcrun simctl get_app_container "$device" nl.slot17.app data)
cp "$container/Documents/slot17.log" "$out/app.log" || status=1
python3 scripts/check_e2e.py "$out/e2e" "$out/app.log" > "$out/e2e-check.txt" 2>&1 || status=1
cat "$out/e2e-check.txt"

TEST_RUNNER_SHOT_DIR="$shots" xcodebuild test-without-building -project Slot17.xcodeproj -scheme Slot17 \
  -destination "id=$device" -derivedDataPath build/test -resultBundlePath "$out/screens.xcresult" \
  -only-testing:Slot17UITests/ScreenshotTests -only-testing:Slot17UITests/WidgetListLook -only-testing:Slot17UITests/QuickDesignTests -only-testing:Slot17UITests/WidgetMarkTests   -parallel-testing-enabled NO > "$out/test-screens.log" 2>&1 || status=1
grep -E "$summary" "$out/test-screens.log" | tail -20

rm -rf "$out/e2e/fake-session"
cp ~/Library/Logs/DiagnosticReports/Slot17* "$out/" 2>/dev/null || true
exit $status
