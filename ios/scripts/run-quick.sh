#!/bin/bash
# Unit tests and pictures of the screens that change most. No pretend drive: that is run-tests.sh.
set -uo pipefail
out=build/check
shots="$PWD/$out/screens"
mkdir -p "$shots"
device=$(xcrun simctl list devices available -j | python3 -c '
import json, re, sys
# The newest iOS first, so the pictures show the current look (Liquid Glass from iOS 26).
def version(runtime):
    return [int(x) for x in re.findall(r"\d+", runtime.split("iOS")[-1])]
for runtime, devices in sorted(json.load(sys.stdin)["devices"].items(), key=lambda kv: version(kv[0]), reverse=True):
    if "iOS" in runtime:
        for d in devices:
            if d["name"].startswith("iPhone"):
                print(d["udid"]); raise SystemExit
raise SystemExit("no iPhone simulator found")')
xcrun simctl boot "$device" || true
xcrun simctl bootstatus "$device" -b >/dev/null
xcodebuild build-for-testing -project Slot17.xcodeproj -scheme Slot17 -destination "id=$device" \
  -derivedDataPath build/test > "$out/build-for-testing.log" 2>&1 \
  || { grep -E "error:" "$out/build-for-testing.log" | head -40; exit 1; }
xcrun simctl install "$device" build/test/Build/Products/Debug-iphonesimulator/Slot17.app
xcrun simctl privacy "$device" grant location nl.slot17.app || true
xcrun simctl location "$device" set 52.3702,4.8952 || true
status=0
# A first launch on its own: does the app run and draw at all, whatever the UI tests then say?
xcrun simctl launch "$device" nl.slot17.app --fresh --demo --screenshot-values --no-audio || status=1
sleep 15
xcrun simctl io "$device" screenshot "$shots/00-first-launch.png" || true
pid=$(pgrep -x Slot17 | head -1 || true)
echo "app process 15 s after launch: ${pid:-none}" | tee "$out/launch-check.log"
if [ -n "$pid" ]; then
  ps -o pid,pcpu,rss,etime,stat -p "$pid" | tee -a "$out/launch-check.log"
  sample "$pid" 3 -file "$out/launch-sample.log" >/dev/null 2>&1 || true
fi
cp ~/Library/Logs/DiagnosticReports/Slot17* "$out/" 2>/dev/null || true
xcrun simctl spawn "$device" log show --last 1m --style compact --predicate 'process == "Slot17"' > "$out/launch-system.log" 2>&1 || true
xcrun simctl terminate "$device" nl.slot17.app || true
bash scripts/check_closing.sh "$device" "$out" || status=1
TEST_RUNNER_SHOT_DIR="$shots" xcodebuild test-without-building -project Slot17.xcodeproj -scheme Slot17 \
  -destination "id=$device" -derivedDataPath build/test -resultBundlePath "$out/results.xcresult" \
  -only-testing:Slot17Tests -only-testing:Slot17UITests/QuickDesignTests -only-testing:Slot17UITests/WidgetMarkTests -parallel-testing-enabled NO \
  > "$out/test.log" 2>&1 || status=1
grep -E 'error:|Test Case .*failed|Executed [0-9]+ tests|\*\* TEST' "$out/test.log" | tail -30
exit $status
