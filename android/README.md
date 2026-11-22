# Slot17 for Android

The Android version of the iPhone app in `ios/`: the same functions, built the Android way (Kotlin,
Jetpack Compose, Material 3) instead of copied screen by screen.

## Layout

| Folder | What |
|---|---|
| `core/` | The rules, without Android: server messages, link timing, sound decisions, status words, sightings, update and fault notices. A port of `ios/Slot17/Core`, tested against `../shared/parity`, which the iOS tests use too. |
| `app/` | The Android app: screens, sound, background service, notifications, widgets and tiles. |
| `scripts/gradle-bootstrap.sh` | Fetches the pinned Gradle with its checksum and makes `./gradlew`, if the wrapper isn't there yet. |

## Building

On Codemagic, workflow **android** (`codemagic.yaml`): the core tests and a debug APK in one build.
Locally, with JDK 17 or newer and the Android SDK: `bash scripts/gradle-bootstrap.sh`, then
`./gradlew :core:test :app:assembleDebug`.

Versions (in `gradle/libs.versions.toml`) are chosen for working together, not for being newest:
Android Gradle Plugin 8.13, Kotlin 2.2, Gradle 8.14.

## iPhone to Android

| iPhone | Android |
|---|---|
| Sound with the screen locked | A foreground service that keeps the link and plays the sounds |
| Live Activity | An ongoing notification (Live Updates on Android 16) |
| Lock Screen and Home Screen widgets | Home screen widgets |
| Control Center controls | Quick Settings tiles |
| Live Activity on CarPlay | Urgent beeps as messages on the Android Auto screen, read out by Android Auto; a spoken reply ("police", "nothing") is a sighting. Settings, Android Auto. The sounds follow the phone's audio to the car either way |
| Liquid Glass | Material 3, dark |

## Status

1.7.0: every screen, the live link, sound with the screen off (foreground service), the ongoing notice,
the home screen widget and two Quick Settings tiles. The build draws every screen as a picture
(`app/build/screenshots`). The release is signed with the key in Codemagic's `android_signing` group.
1.8.0: urgent beeps on Android Auto (`AutoMessages.kt`, rules in `core/CarMessages.kt`).
Published with release 1.3.0 as `Slot17.apk` (1.7.0). Still to do: a drive with a real phone, and running
`shared/parity` in the iOS tests too.
1.9.0: Bluetooth to the car computer (`BluetoothLink.kt`, `BluetoothRoute.kt`; the frames in `core/Bluetooth.kt`,
tested against `shared/parity/ble-frames.json`): companion device pairing, a GATT link that reconnects by itself,
and the Settings card. Not yet: starting the app when the car computer appears.
