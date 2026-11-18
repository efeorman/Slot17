# Security

Slot17 is a receive-only project. It has no transmit path and does not decrypt or listen to any content.

## Reporting a problem

Don't open a public issue for a security problem. Use **Report a vulnerability** on the repository's Security tab, if it's enabled, or contact the owner privately through GitHub.

## How it is protected

| Layer | Protection |
|---|---|
| Keys | The server creates random keys, stores them with owner-only access outside Git, and checks them in constant time |
| Requests | Size limits, deadlines for headers and bodies, and limits per connection and address |
| Input | Every value is rebuilt from known, typed, bounded fields; wrong types and invalid numbers are refused |
| Browser | A strict content security policy, an HttpOnly cookie, and same-origin checks on commands |
| Commands | Each one is single use, and stop and power off expire so they can't fire on a later drive. A sighting and a Pushover switch wait up to a day, so they reach the car computer on its next drive; neither can start or stop anything |
| The Pi | Accepts no incoming connections for this. Its two permissions allow one exact command each, and there is no start or reboot command |
| Credentials | Pushover keys exist only in memory when sending, and never in the queue on disk |
| Logs | Uploaded logs get names chosen by the server, with limits on count and size |

## Known weaknesses

These are accepted for the current private use. **Fix the first two before anyone else relies on this.**

- **Plain HTTP:** keys, locations and commands travel unencrypted. Anyone who can watch the connection can read the keys and use them.
- **One viewer key does everything:** the same key views data, stops the detector and powers off the Pi.
- **Distributed attacks:** the limits hold against one attacking machine in local tests, but a large distributed attack would still overwhelm the server.
- **Sighting rate:** there's no separate rate limit on sightings for someone who holds the key.
- **App transport:** the app allows plain HTTP (`NSAllowsArbitraryLoads`) because the server uses it.

## Running a server

- Run it as an ordinary user, never as root.
- Keep `config.json` at mode `0600`.
- If your link has been shared, make new keys: remove `/opt/slot17-display/config.json`, run `bash setup.sh server` again, then run `bash setup.sh car --server ADDRESS --pi-key NEW_KEY` on the car computer and paste the new link into the app.
- Open only port 5732 for this service.
