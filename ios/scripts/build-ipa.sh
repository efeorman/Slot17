#!/bin/bash
# Builds for a real iPhone without a real signature; Signulous or Sideloadly signs it on install. The app and
# its widget get an ad-hoc signature that carries the App Group, so the re-signing sees which group to keep.
set -euo pipefail
mkdir -p build/check
xcodebuild archive -project Slot17.xcodeproj -scheme Slot17 -configuration Release \
  -destination 'generic/platform=iOS' -archivePath build/Slot17.xcarchive \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= > build/check/archive.log 2>&1 \
  || { grep -E "error:|\*\* " build/check/archive.log | head -60; exit 1; }
app=build/Slot17.xcarchive/Products/Applications/Slot17.app
for framework in "$app"/Frameworks/*; do
  [ -e "$framework" ] && codesign --force --sign - --timestamp=none "$framework"
done
codesign --force --sign - --timestamp=none --entitlements Slot17Live/Slot17Live.entitlements "$app/PlugIns/Slot17Live.appex"
codesign --force --sign - --timestamp=none --entitlements Slot17/Slot17.entitlements "$app"
codesign -d --entitlements - "$app/PlugIns/Slot17Live.appex" 2>&1 | grep -q group.nl.slot17.app
codesign -d --entitlements - "$app" 2>&1 | grep -q group.nl.slot17.app
/usr/libexec/PlistBuddy -c 'Print :UIBackgroundModes:0' "$app/Info.plist" | grep -qx audio
/usr/libexec/PlistBuddy -c 'Print :NSAppTransportSecurity:NSAllowsArbitraryLoads' "$app/Info.plist" | grep -qx true
/usr/libexec/PlistBuddy -c 'Print :CFBundleIcons:CFBundlePrimaryIcon:CFBundleIconName' "$app/Info.plist" | grep -qx AppIcon
test -f "$app/Assets.car"
# The Live Activity (Lock Screen, Dynamic Island, CarPlay Dashboard) lives in this extension.
test -d "$app/PlugIns/Slot17Live.appex"
/usr/libexec/PlistBuddy -c 'Print :NSSupportsLiveActivities' "$app/Info.plist" | grep -qx true
/usr/libexec/PlistBuddy -c 'Print :NSExtension:NSExtensionPointIdentifier' "$app/PlugIns/Slot17Live.appex/Info.plist"   | grep -qx com.apple.widgetkit-extension
# The private link must never be built in, and neither may the test harness's key.
if strings "$app/Slot17" "$app/PlugIns/Slot17Live.appex/Slot17Live" | grep -E "e2e-viewer|192\.253\.211"; then echo "a secret or address is in the app"; exit 1; fi
rm -rf build/ipa && mkdir -p build/ipa/Payload
cp -R "$app" build/ipa/Payload/
(cd build/ipa && zip -qry ../Slot17-unsigned.ipa Payload)
ls -l build/Slot17-unsigned.ipa
