# Internet from a 4G USB stick instead of a phone hotspot

The car computer needs internet to send what it hears to the display server. A
phone hotspot works and needs nothing extra. A 4G USB stick with its own SIM does
the same job without the phone getting warm or its battery draining. This page
records the one stick that has been tested and what it took.

**Status:** tested at home on one Raspberry Pi 5 on 14 November 2026, and on two
drives that evening (56 and 22 minutes). It worked for all but eight minutes of
them; see "The first drives" below. Read "Use a USB port on the other
controller" before you choose a socket, and leave your hotspot switched on until
you trust the stick.

## Setting it up

With the stick plugged into the car computer, the detector stopped, and another
internet connection still working:

```bash
bash ~/Slot17/setup.sh modem
```

That does both steps described below and then checks that internet works over
the stick. `bash ~/Slot17/setup.sh modem --check` shows the state without
changing anything, and `--remove` takes it all out again. Running it a second
time is harmless, and can be done with the detector running.

Related: [README.md](../../README.md) for the normal hotspot setup.

## What happens with each network

| Network present | Used for internet |
|---|---|
| Wired (at home) | Wired |
| No wire, stick has a data session | The stick |
| No wire, stick absent or without a data session | Wi-Fi, for example the phone hotspot |

The hotspot stays configured as the fallback. Nothing has to be switched by hand.

## Tested stick: ZTE MF833N

Firmware `BD_LVWRGBMF833NV1.0.0B06`, hardware `MF833N-HW1.0`. It appears on USB
as `19d2:1225` (a CD-ROM), and after a mode switch as `19d2:1403` (network).

**It does not work on a Raspberry Pi 5 out of the box.** The Pi asks the stick to
activate (`SET_CONFIGURATION`). The stick activates but never confirms, so Linux
gives up and no network interface appears. The kernel log shows
`can't set config #1, error -110` or `error -71`. Another port, another cable or
waiting does not help. On a Windows PC the same stick works.

### What the setup installs, part 1: the USB fix (someone else's project)

[hamacherengineering/zte-mf79n-pi5-fix](https://github.com/hamacherengineering/zte-mf79n-pi5-fix)
was written for the sister model MF79N and works for the MF833N unchanged
(version 0.2.0, commit `1e75ba9`). The setup fetches exactly that version, which
is the one we read and tested, and nothing newer. In short:

- It adds two small kernel modules through DKMS. One accepts the activation when
  the stick itself reports that it is active. The other is a network driver for
  the stick's Windows-style (RNDIS) mode.
- It is experimental and tied to the kernel's tracing interface. A kernel update
  can stop it building; the stick then stops working until the fix is updated.
  The detector does not depend on it.
- The first module stays in memory until the next reboot.

The setup keeps its source in `~/zte-mf79n-pi5-fix`. Leave that folder there:
removing the fix needs it.

### What the setup installs, part 2: keep the data session up and rank the route

With the USB fix the stick is a network interface, but some things are left:

- The stick does not always open its mobile data session by itself, although it
  is set to connect automatically. Until it does, there is no internet.
- It can get stuck in "connecting" and stay there until it is replugged.
- With some SIMs it will not connect at all while data roaming is off. Ours
  (Lycamobile in the Netherlands) reports its home network as "Internal"
  roaming.
- The fix gives the stick the same rank as a wired network and makes it the only
  DNS server, so with no data session nothing resolves and the hotspot is never
  used.

`deploy/modem_link.py` handles these. It runs every 30 seconds from
`slot17-modem.timer` and:

- keeps data roaming switched on, with the same command the stick's own page uses
  (set `SLOT17_MODEM_ROAMING=leave` to stop that);
- asks the stick to connect when its session is down; after 2 minutes without a
  session it drops and redials; after 5 minutes it asks the stick to restart
  itself, which is the same as a replug (at most once every 15 minutes);
- ranks the route: metric 200 with a session (after wired at 100, before Wi-Fi at
  600), metric 700 without one (after Wi-Fi);
- writes a line about the link every five minutes, and at once when the cell
  changes, packets are lost, or the session drops or comes back: network, band,
  signal (RSRP, RSRQ, SNR), cell, and the round trip to the display server in
  milliseconds.

The car computer's publisher (since 1.1.3) also does its part: a push that fails
is tried again at once over every other network with a default route, so with
the hotspot on, a stall on the stick costs a fraction of a second rather than a
gap. Once a minute it writes a `link:` line with the number of pushes, failures,
median and slowest milliseconds and the network used, and it notes when the
clock gets its network time. Both kinds of line arrive in the Pi log the server
collects every ten minutes.

If your wired or Wi-Fi metrics are not NetworkManager's defaults, set
`SLOT17_MODEM_METRIC_UP` and `SLOT17_MODEM_METRIC_DOWN` in
`/etc/systemd/system/slot17-modem.service`.

### Checking it

```bash
bash ~/Slot17/setup.sh modem --check
ip route | grep default                      # the stick's line shows metric 200
journalctl -u slot17-modem.service -b | grep modem:
journalctl -u zte-mf79n.service -b | grep event
```

Measured on the test Pi after a reboot: the network interface was up about 40 seconds
after boot, and the data session about 95 seconds after boot, on the second
attempt. So the first minute and a half of a drive has no internet from the
stick; the detector records regardless and sends when the link is there.

What the check writes to the log (from version 1.1.3 with what the stick says
about its reception, which is what tells "no coverage" from "would not connect"):

| Line | Meaning |
|---|---|
| `modem: data session was down, asked the stick to connect` | The stick had no mobile connection and was asked to make one |
| `modem: data session up after 4 s (LTE, 3 bars, RSRP -107 dBm)` | It connected, and how good the reception was |
| `modem: no data session (ppp_connecting; NO_SERVICE, 0 bars)` | 20 seconds later it still had none; the check tries again in 30 seconds |
| `modem: route metric set to 700 on eth1` | Internet now goes over Wi-Fi if there is one; `200` means over the stick again |
| `modem: status page not reachable` | The stick did not answer, for example while it restarts |
| `modem: data roaming was off; switched it on` | Roaming had been switched off, on the stick's page or by a reset |
| `modem: no data session for 120 s; dropping and redialling` | The first step when a session stays down |
| `modem: ... asked the stick to restart itself` | The second step, five minutes down: like a replug |
| `modem: link up (LTE, band 20, 3 bars, RSRP -107 dBm, RSRQ -12 dB, SNR -1.0 dB, cell 24c315/177); round trip to ... 48 ms (worst 90 ms) [regular report]` | How the link is; the reason for the line is in brackets |
| `link: 30 pushes in 60 s, 0 failed, median 52 ms, slowest 140 ms, via default 30` | From the publisher: how the pushes to the server went in the last minute |
| `push went over wlan0 after a failure on eth1` | A push stalled on the stick and went over the hotspot instead |

## The first drives (14 November 2026)

Two sessions with the stick as the only internet (the hotspot was off), read
from the car computer's own log and the phone's.

- **Start:** internet over the stick 93 seconds after power-up on the first
  drive, on the second connection request.
- **While driving** (motorway speeds): four times one upload to the server
  timed out and the next one went through. The phone warns after 30 seconds
  without data and did not warn for these.
- **Once, while parked: eight minutes without internet.** The stick dropped its
  mobile connection. It was asked to reconnect every 30 seconds and answered
  "connecting" for seven minutes, with its light red, until it was **pulled out
  and plugged back in by hand**; then it connected at the first request. The
  phone showed no data from the car for that time. Data roaming was off on the
  stick, and our SIM's home network counts as roaming ("Internal"), which is the
  likeliest cause; it is now kept on. **Not shown:** the log of that day did not
  record the reception. Since 1.1.3 the check redials after 2 minutes and
  restarts the stick after 5 by itself, so a replug should no longer be needed.
- **Dropped samples at the start**, with stick and receiver on the same USB
  controller: 18, 4 and 14 buffers in the first three minutes of three sessions,
  none in a fourth, and none later in any of them. See the next section.

- **Again on 17 November**, same controller: three of four power-ups lost
  0.1 s once or a few times, 0.3 to 19 s after the stream opened, and nothing
  later. Since 1.2.5 such losses in the first minute are counted as
  `startup_dropped` and no longer mark the drive degraded.
- **The stick's transmitter does reach the receiver** (15 November, at home with
  no antenna): pings over the stick raised the detector's candidates from 12 to
  34 a minute to 488 to 915 a minute, in the rhythm of the pings. These are the
  stick's own LTE uplink bursts, the same 1 ms pattern as the whole-band
  interference seen on drives.

**Not yet shown:** that the other USB controller avoids the dropped samples;
how much of the interference on a drive is the stick's and how much the
phone's; whether a lead is enough; and how often the stick loses its
connection over many drives.

## Things to know

- **The stick's settings page** is at `http://192.168.0.1` from whatever computer
  it is plugged into. On this firmware the connect command needs no login from
  that side, which is what the script relies on.
- **Nothing can connect inwards.** The mobile network gives the stick a private
  address. The car computer reaches the display server; you cannot SSH to the
  car computer over the stick. Use the hotspot or a wired network for that.
- **It is a transmitter next to a receiver.** A 4G stick sends on its own
  frequencies a few centimetres from the SDR. Put it on a short USB extension
  lead away from the SDR and its antenna, and compare the interference figure in
  the app with and without it. At home it measurably reaches the receiver (see
  above); on a drive the effect of a lead has not been measured yet.
- **Use a USB port on the other controller.** A Pi 5 has two USB controllers,
  each serving two sockets. `lsusb -t` shows which bus each device is on: the
  Airspy and the stick should be under different `Bus` lines. With both on the
  same bus, the receiver dropped 18, 4 and 14 buffers (up to a third of a second
  of signal) in the first three minutes of three sessions out of four, while
  the stick was starting up; sessions before the stick dropped none or one.
  Dropped samples also put the detector's candidate checking on one thread:
  until 1.1.2 for the rest of the session, which on one drive left 57% of
  candidates unreached, from 1.1.3 for five minutes, and from 1.2.5 not at all
  for losses in the stream's first minute. Whether the other
  controller avoids the loss is not yet measured: a few minutes after a start,
  look at `grep -o '"dropped": [0-9]*' /run/tetra/watch.heartbeat`.
- **Leave the hotspot switched on at first.** It costs nothing while the stick
  works, because the stick ranks above Wi-Fi, and it carries the data when the
  stick has no connection.
- **A red light on the stick** means it has no mobile connection: at every
  start for the first minute or so, and whenever it loses the network. If it
  stays red for minutes where your phone has coverage, pull the stick out and
  put it back.
- **Once, the receiver was missing after a restart.** On one start in the car
  the Airspy did not appear on USB at all and the detector did not run; the
  stick was on the same controller. This receiver has also failed to appear
  after short power interruptions before the stick existed, so the stick is
  not shown to be the cause. Switching the car computer fully off and on again
  brought it back.
- **Other sticks.** Any stick that Linux sees as a network interface by itself
  needs no setup: plug it in and look at `ip -br addr`. The session check is
  specific to ZTE's status page, and the USB fix to three ZTE USB IDs
  (`19d2:1225`, `19d2:1403`, `19d2:1405`).
- **Kernel updates.** After a system update that brings a new kernel, run
  `bash ~/Slot17/setup.sh modem --check`. If the USB fix shows as not installed,
  its modules did not build for the new kernel.
