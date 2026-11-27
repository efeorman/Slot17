# Slot17 FAQ

The questions we get most, with short answers. The [README](https://github.com/efeorman/Slot17#readme) has the full setup, and if you're still stuck, ask in the [Discord](https://discord.gg/rSFPWSWNfh).

<!-- github-only -->
**Topics:** [The basics](#the-basics) · [What it can and can't tell you](#what-it-can-and-cant-tell-you) · [Hardware](#hardware) · [Countries and the law](#countries-and-the-law) · [Setting it up](#setting-it-up) · [The apps](#the-apps) · [Help and helping out](#help-and-helping-out)
<!-- /github-only -->

## The basics

### What is Slot17?
A Raspberry Pi with a radio receiver (we use an Airspy Mini) that listens to the TETRA band, 380 to 385 MHz. It looks for the training pattern that's inside every TETRA burst, and your phone shows signal bars and beeps when it picks up a strong one. It only receives.

### Is it a police detector?
No. Police, ambulances, the fire brigade and a lot of others (in the Netherlands also KMar, Rijkswaterstaat, ProRail, customs and more) all use the same TETRA network. Slot17 can't tell them apart, and a vehicle whose radio stays quiet is invisible to it.

### How is it different from detectors that only measure power?
A lot of DIY projects just measure how much power is in the band. Then LTE, a phone charger or plain noise sets them off all the time. Slot17 checks for the TETRA training pattern from the ETSI standard, which noise and other systems don't have. In our tests it gave 0 false matches on 338,438 test signals and recordings without TETRA.

### What does it cost?
The software is free, and so is a hosted server from us. You pay for the hardware: a Raspberry Pi 5, a receiver (we use an Airspy Mini), an antenna, storage and a good car charger. Count on a few hundred euros, depending on what you already have.

### Can I sell it or use it in a business?
No. It's under the [PolyForm Noncommercial licence](https://github.com/efeorman/Slot17/blob/main/LICENSE): free for personal use, research, hobby projects, schools and charities, not for selling or commercial use.

## What it can and can't tell you

### How far away does it hear a radio?
It depends on your antenna, your car and the buildings or land in between. 380 MHz from a few watts reaches kilometres over a clear path, so it often hears radios you can't see. A beep means a radio transmitted and the signal was strong, not that it's close.

### Isn't a stronger signal a closer radio?
Not with TETRA. A radio sets its own power by how well it hears its mast, over a range of 30 dB. That's a factor of 32 in distance. A handheld 200 m away next to a mast can reach you weaker than a van 3 km away at the edge of the cell.

### Do TETRA radios transmit all the time?
No. A radio only transmits when it has something to say: when it's switched on, moves into another cell, sends a status or location message, or someone talks. The "every 4 seconds" you read online isn't in the standard. On our drives the gap between matches on one channel was usually around 21 seconds, but anywhere from half a second to 4.5 minutes. A radio that's idle sends nothing.

### Why does it beep when there's nothing in sight?
One mast covers a big area (about 68 km² on average in the Netherlands) with on average around 140 radios in it, and radios in buildings, stations, parked cars and on people register too. It's a real TETRA burst, just not one next to you.

### Does it see helicopters?
Yes, when they're in the air. Police and ambulance helicopters above about 150 m use their own TETRA channels, set aside across Europe for emergency services' aircraft, and Slot17 marks a match on them (L190 to L199) with a helicopter and the word Aircraft. We checked our own drives against the public flight tracks of the Dutch police and trauma helicopters: in minutes with a match on those channels one of them was flying within 20 km half the time, against 4 % in other minutes. A helicopter that is landing or hovering low uses the normal channels, like a car. You can switch their sound off in Settings and still see them.

### Does it see a traffic check where radios talk to each other directly?
No. Radios talking without a mast use Direct Mode, a different standard. Slot17 doesn't recognise it.

### Can it listen to calls or decode anything?
No. It never decodes, decrypts or transmits anything. It only checks whether a burst has the TETRA training pattern. The traffic is encrypted anyway, and decoding it is illegal.

## Hardware

### Which receiver do I need? Can I use an RTL-SDR?
The Airspy Mini is the one we drive with: it takes in the whole 5 MHz band at once, all 200 channels. Since 1.7.0 the software also works with an SDRplay RSP1B or RSP1A, or with a pair of RTL-SDR sticks (one stick alone only covers about half the band reliably, so it takes two). [RECEIVERS.md](https://github.com/efeorman/Slot17/blob/main/docs/design/RECEIVERS.md) explains each before you buy one. Other receivers aren't supported.

### Which computer?
A Raspberry Pi 5. We use the 8 GB one; 2 GB and up should be fine, the detector uses about 0.6 GB. We use the Pi 5; slower computers may not keep up.

### Antenna inside or outside the car: is there a big difference?
Yes. An antenna on the outside, like a magnetic roof antenna, hears more than one behind the windscreen, and some windscreens have a metal coating that blocks radio. Keep in mind that more range also means more beeps for radios you can't see. We use a Motorola GMAE4258B stuck inside the windscreen. Anything made for 380 to 400 MHz works.

### What power supply?
We use the UGREEN Nexode 60 W car charger (EC602). It gives 3 A at 5 V, which was enough for our Pi with the receiver, an SSD and a 4G stick. Bigger car chargers don't give the Pi more: their extra watts are at higher voltages. If Slot17 reports undervoltage, your setup needs more: use a 12 V to 5 V converter rated for 5 A, as the [README](https://github.com/efeorman/Slot17#what-you-need) explains. Where to buy the parts in your country, with prices and a total: [slot17.cloud/parts/](https://slot17.cloud/parts/).

### Do I need internet in the car?
Not with Bluetooth on (since 1.6.0). The car computer then sends everything straight to the app, and the app's buttons work over it too. Since 1.8.0 you don't even need a server or internet to set it up: a pairing code from the app does it. Without internet in the car you miss the web page, the car computer's part of the drive report and support packages; updates come when the car computer starts on your home Wi-Fi. Without Bluetooth, yes: the car computer reaches the server over your phone's hotspot, or a 4G USB stick with its own SIM, which saves your phone's battery (we use a ZTE MF833N). The [README](https://github.com/efeorman/Slot17#bluetooth-or-the-server) compares the two.

### What happens when I switch the car off?
It just loses power, and that's fine. It starts again by itself the next time the car computer gets power. [POWER-LOSS.md](https://github.com/efeorman/Slot17/blob/main/docs/operations/POWER-LOSS.md) explains what that keeps.

## Countries and the law

### Will it work in my country?
It's built for TETRA in 380 to 385 MHz, the band most of Europe uses for emergency services.
- **In daily drives:** the Netherlands, Belgium and Germany.
- **Should work:** the United Kingdom, Austria and the Nordic countries.
- **Won't work:** France and Switzerland (TETRAPOL, a different system), and the US and Canada, where public safety mostly uses P25.
- **Elsewhere, including eastern Europe:** check which system your emergency services use and on which band. If it's TETRA in 380 to 385 MHz, it should work. Tell us how it goes in the Discord.

### Is it legal?
It only receives, and it never decodes, decrypts or transmits anything. The rules for receiving this band, or for using something like this in a car, are different per country, so check yours before you use it. See [LEGAL.md](https://github.com/efeorman/Slot17/blob/main/LEGAL.md).

## Setting it up

### Do I need to know Linux?
No. Use the ready-made image: flash it with Raspberry Pi Imager, fill in one text file on the card, and start the Pi. [BUILD.md](https://github.com/efeorman/Slot17/blob/main/docs/BUILD.md) walks through it. Doing it by hand is one install line, and the [README](https://github.com/efeorman/Slot17#readme) goes step by step. Not used to it? [SETUP-WITH-AI.md](https://github.com/efeorman/Slot17/blob/main/docs/SETUP-WITH-AI.md) has a prompt that lets any AI chat assistant walk you through it.

### Does it need a server?
Not since 1.8.0, if you use Bluetooth: the app makes a pairing code, you give it to the car computer once, and the phone then gets everything straight from it in the car ([README: Without a server](https://github.com/efeorman/Slot17#without-a-server)). A server is for the rest. The Pi sits behind your phone's hotspot, so your phone can't reach it over the internet, but both can reach a small server: that gives you the web page on any phone or computer, also from home, drive reports, support packages and the apps' notice of a new version. Updates for the car computer come without a server too, straight from GitHub, when it starts with internet.

### Your server or my own?
Ours is the easy way: type `/server create` in the Discord and you get your own address on slot17.cloud with HTTPS and your own keys, linked to your Discord account. Want to keep everything yourself? Rent a cheap Linux VPS and run one command. Both work the same in the app. [Step 2 of the README](https://github.com/efeorman/Slot17#step-2-set-up-the-display-server) has both.

### What does a hosted server store, and who can see it?
What your car computer and app send it: the latest state of the detector, and the drive data and logs you send after a drive. Only you have the keys. `/server info` shows how it's doing, `/server delete` removes everything straight away. The details are on [slot17.cloud/privacy.html](https://slot17.cloud/privacy.html).

### I lost my keys or shared them by accident
On a hosted server, type `/server new-keys` in the Discord. The old link and car computer key stop working, so put the new ones in the app and on the car computer. On your own server, `bash ~/Slot17/setup.sh server --show` prints lost keys again. Shared them? Running the installer again keeps the old keys; [SECURITY.md](https://github.com/efeorman/Slot17/blob/main/SECURITY.md#running-a-server) says how to make new ones.

### How do I update?
From 1.3.1 the car computer installs new versions by itself, if you said yes to that in its setup (the default). Otherwise run the same install line again; it keeps your keys, settings, calibration and recordings. The Android app updates itself from Settings, About; the iPhone app you install again the same way. See [Updating](https://github.com/efeorman/Slot17#updating).

## The apps

### Is there an app?
Yes, for iPhone and for Android, and the web page works on any phone or computer. Both apps are on the [latest release](https://github.com/efeorman/Slot17/releases/latest).

### How do I install the iPhone app?
It isn't in the App Store, so you sign it yourself with Signulous (paid, works for a year) or Sideloadly (free, signs it for 7 days and can sign it again by itself). The [README's step 4](https://github.com/efeorman/Slot17#iphone) explains both.

### How do I install the Android app?
Open **Slot17.apk** from the latest release on your phone and allow the install. Then in the app's Settings, tap **Let Slot17 run in the background**, so it keeps beeping with the screen off.

### Does it work with CarPlay or Android Auto?
The sound comes through your car either way. On CarPlay the iPhone shows a Live Activity with the status (iOS 26). In Android Auto an urgent beep shows as a message from Slot17 that Android Auto reads out, and you can say "police", "ambulance", "fire" or "nothing" to mark what you saw. For that, turn on **Unknown sources** in Android Auto's developer settings once (tap **Version** 10 times in the Android Auto settings), because the app isn't from Google Play. Don't want the messages? Switch them off in the app's Settings, Android Auto.

### Why do I have to open the iPhone app before driving?
iOS doesn't let an app start sound from the background. Open it once, then lock the phone; that's what arms the sound.

## Help and helping out

### Something doesn't work
Open the app's Pi screen: the **Setup check** shows every link from your phone to the receiver, and the car computer's own check, with what to do about each problem. Or run `bash ~/Slot17/setup.sh doctor` on the car computer. It gives a plain verdict per part, and the first problem is usually the cause of the rest. Paste the output in the Discord or in a [GitHub issue](https://github.com/efeorman/Slot17/issues).

### How can I help?
Test it in your country, report bugs, and post your setup and clips in the Discord. If you drive with a passenger, mark what you see with **I SAW** in the app, including vehicles that gave no beep. That's the only way to learn what Slot17 misses.
