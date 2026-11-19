# Slot17 FAQ

The questions we get most, with short answers. The [README](https://github.com/efeorman/Slot17#readme) has the full setup, and if you're still stuck, ask in the [Discord](https://discord.gg/rSFPWSWNfh).

<!-- github-only -->
**Topics:** [The basics](#the-basics) · [What it can and can't tell you](#what-it-can-and-cant-tell-you) · [Hardware](#hardware) · [Countries and the law](#countries-and-the-law) · [Setting it up](#setting-it-up) · [The apps](#the-apps) · [Help and helping out](#help-and-helping-out)
<!-- /github-only -->

## The basics

### What is Slot17?
A Raspberry Pi with an Airspy Mini receiver that listens to the TETRA band, 380 to 385 MHz. It looks for the training pattern that's inside every TETRA burst, and your phone shows signal bars and beeps when it picks up a strong one. It only receives.

### Is it a police detector?
No. Police, ambulances, the fire brigade and a lot of others (in the Netherlands also KMar, Rijkswaterstaat, ProRail, customs and more) all use the same TETRA network. Slot17 can't tell them apart, and a vehicle whose radio stays quiet is invisible to it.

### How is it different from detectors that only measure power?
A lot of DIY projects just measure how much power is in the band. Then LTE, a phone charger or plain noise sets them off all the time. Slot17 checks for the TETRA training pattern from the ETSI standard, which noise and other systems don't have. In our tests it gave 0 false matches on 338,438 test signals and recordings without TETRA.

### What does it cost?
The software is free, and so is a hosted server from us. You pay for the hardware: a Raspberry Pi 5, an Airspy Mini, an antenna, storage and a good car charger. Count on a few hundred euros, depending on what you already have.

### Can I sell it or use it in a business?
No. It's under the [PolyForm Noncommercial licence](https://github.com/efeorman/Slot17/blob/main/LICENSE): free for personal use, research, hobby projects, schools and charities, not for selling or commercial use.

## What it can and can't tell you

### How far away does it hear a radio?
We haven't measured that yet. 380 MHz from a few watts reaches kilometres over a clear path, so it often hears radios you can't see. A beep means a radio transmitted and the signal was strong, not that it's close.

### Isn't a stronger signal a closer radio?
Not with TETRA. A radio sets its own power by how well it hears its mast, over a range of 30 dB. That's a factor of 32 in distance. A handheld 200 m away next to a mast can reach you weaker than a van 3 km away at the edge of the cell.

### Do TETRA radios transmit all the time?
No. A radio only transmits when it has something to say: when it's switched on, moves into another cell, sends a status or location message, or someone talks. The "every 4 seconds" you read online isn't in the standard. On our drives the gap between matches on one channel was usually around 21 seconds, but anywhere from half a second to 4.5 minutes. A radio that's idle sends nothing.

### Why does it beep when there's nothing in sight?
One mast covers a big area (about 68 km² on average in the Netherlands) with on average around 140 radios in it, and radios in buildings, stations, parked cars and on people register too. It's a real TETRA burst, just not one next to you.

### Does it see a traffic check where radios talk to each other directly?
No. Radios talking without a mast use Direct Mode, a different standard. Slot17 doesn't recognise it.

### Can it listen to calls or decode anything?
No. It never decodes, decrypts or transmits anything. It only checks whether a burst has the TETRA training pattern. The traffic is encrypted anyway, and decoding it is illegal.

## Hardware

### Which receiver do I need? Can I use an RTL-SDR?
An Airspy Mini. It takes in the whole 5 MHz band at once, all 200 channels. An RTL-SDR only does about half of that reliably, and the code only works with the Airspy. Other receivers aren't supported.

### Which computer?
A Raspberry Pi 5. We use the 8 GB one; 2 GB and up should be fine, the detector uses about 0.6 GB. Only the Pi 5 has been tested; slower computers may not keep up.

### Antenna inside or outside the car: is there a big difference?
Yes. An antenna on the outside, like a magnetic roof antenna, hears more than one behind the windscreen, and some windscreens have a metal coating that blocks radio. Keep in mind that more range also means more beeps for radios you can't see. We use a Motorola GMAE4258B stuck inside the windscreen. Anything made for 380 to 400 MHz works.

### What power supply?
A USB-C car charger that gives 5 V at 5 A. Most only give 3 A at 5 V, whatever the box says, and that causes undervoltage, which Slot17 reports.

### Do I need internet in the car?
Yes, so the car computer can reach the server. Your phone's hotspot is enough. A 4G USB stick with its own SIM saves your phone's battery; we use a ZTE MF833N.

### What happens when I switch the car off?
It just loses power, and that's fine. It starts again by itself the next time the car computer gets power. [POWER-LOSS.md](https://github.com/efeorman/Slot17/blob/main/docs/operations/POWER-LOSS.md) explains what that keeps.

## Countries and the law

### Will it work in my country?
It's built for TETRA in 380 to 385 MHz, the band most of Europe uses for emergency services. Only the Netherlands is tested so far.
- **Should work:** Belgium, Germany, the United Kingdom, Austria and the Nordic countries.
- **Won't work:** France and Switzerland (TETRAPOL, a different system), and the US and Canada, where public safety mostly uses P25.
- **Elsewhere, including eastern Europe:** check which system your emergency services use and on which band. If it's TETRA in 380 to 385 MHz, it should work. Tell us how it goes in the Discord.

### Is it legal?
It only receives, and it never decodes, decrypts or transmits anything. The rules for receiving this band, or for using something like this in a car, are different per country, so check yours before you use it. See [LEGAL.md](https://github.com/efeorman/Slot17/blob/main/LEGAL.md).

## Setting it up

### Do I need to know Linux?
It helps, but you don't have to. The installer is one line and the [README](https://github.com/efeorman/Slot17#readme) goes step by step. Not used to it? [SETUP-WITH-AI.md](https://github.com/efeorman/Slot17/blob/main/docs/SETUP-WITH-AI.md) has a prompt that lets any AI chat assistant walk you through it.

### Why does it need a server?
The Pi sits behind your phone's hotspot, so your phone can't reach it directly. Both can reach a small server on the internet, and that's all the server is for.

### Your server or my own?
Ours is the easy way: type `/server create` in the Discord and you get your own address on slot17.cloud with HTTPS and your own keys, linked to your Discord account. Want to keep everything yourself? Rent a cheap Linux VPS and run one command. Both work the same in the app. [Step 2 of the README](https://github.com/efeorman/Slot17#step-2-set-up-the-display-server) has both.

### What does a hosted server store, and who can see it?
What your car computer and app send it: the latest state of the detector, and the drive data and logs you send after a drive. Only you have the keys. `/server info` shows how it's doing, `/server delete` removes everything straight away. The details are on [slot17.cloud/privacy.html](https://slot17.cloud/privacy.html).

### I lost my keys or shared them by accident
Type `/server new-keys` in the Discord. The old link and car computer key stop working, so put the new ones in the app and on the car computer.

### How do I update?
Run the same install line again. It keeps your keys, settings, calibration and recordings. The app tells you when there's a new version. See [Updating](https://github.com/efeorman/Slot17#updating).

## The apps

### Is there an app?
Yes, for iPhone and for Android, and the web page works on any phone or computer. Both apps are on the [latest release](https://github.com/efeorman/Slot17/releases/latest).

### How do I install the iPhone app?
It isn't in the App Store, so you sign it yourself with Signulous (paid, works for a year) or Sideloadly (free, reinstall every 7 days). The [README's step 4](https://github.com/efeorman/Slot17#iphone) explains both.

### How do I install the Android app?
Open **Slot17.apk** from the latest release on your phone and allow the install. Then in the app's Settings, tap **Let Slot17 run in the background**, so it keeps beeping with the screen off.

### Does it work with CarPlay or Android Auto?
The sound comes through your car either way. On CarPlay the iPhone shows a Live Activity with the status (iOS 26). On Android the sound follows the phone's audio over Bluetooth or Android Auto; there's no Android Auto screen.

### Why do I have to open the iPhone app before driving?
iOS doesn't let an app start sound from the background. Open it once, then lock the phone; that's what arms the sound.

## Help and helping out

### Something doesn't work
Run `bash ~/Slot17/setup.sh doctor` on the car computer. It gives a plain verdict per part, and the first problem is usually the cause of the rest. Paste the output in the Discord or in a [GitHub issue](https://github.com/efeorman/Slot17/issues).

### How can I help?
Test it in your country, report bugs, and post your setup and clips in the Discord. If you drive with a passenger, mark what you see with **I SAW** in the app, including vehicles that gave no beep. That's the only way to learn what Slot17 misses.
