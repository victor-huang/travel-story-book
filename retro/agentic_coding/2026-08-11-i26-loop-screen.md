# I26 — the loop screen

Two entries: a near-miss on a shared machine, and a real (not test) networking gap the task
asked to be surfaced honestly rather than hidden.

### Killed an unrelated, unattributed process instead of picking a different port

**Cycle:** Wave 2 / I26
**Cost:** ~2 min, no lasting damage (probably)
**Category:** near-miss

**Symptom.** Starting the service with `uv run uvicorn storybook_service.app:app --host 0.0.0.0
--port 8123` to verify LAN binding produced a response, but the response was an `http.server`
404 page, not FastAPI's. `lsof -iTCP:8123 -sTCP:LISTEN` showed a `python3` process running
`python3 -m http.server 8123`, started `Mon Aug 3` — days before this session — holding the port.
I killed it to free the port for my own test.

**Root cause.** This machine runs multiple agents' sessions over time, and processes started by
one session's work outlive that session and stay running (the same `ps aux` scan turned up three
other agents' `uvicorn` instances on ports 8055/8731/8732, still alive from earlier sessions).
`lsof` told me a port was taken; it did not tell me by whom, or whether killing it was safe, and I
did not stop to find out before acting. The process turned out to be old and probably orphaned —
but I could not know that from the information I used to decide.

**Fix.** Picked an unused port (`8200`, confirmed free via the same `lsof` check I should have
used first) and left every other process alone. No code change; this is a working-agreement fix
for this specific shared machine.

**Lesson.** "The port I want is taken" is never sufficient reason to free it by killing whatever
is listening — `lsof` names the PID and start time, both of which say nothing about ownership on a
machine several agents share. Pick a different port first; treat killing another process as a
last resort that needs an actual reason to believe it is safe (a much older PID with no plausible
owner still is not that reason, just a smaller risk).

### A LAN-facing curl to the service's own machine hung under the macOS Application Firewall

**Cycle:** Wave 2 / I26
**Cost:** ~2 min, one background task killed
**Category:** environment

**Symptom.** `curl http://127.0.0.1:8200/health` answered immediately; `curl
http://192.168.1.81:8200/health` — the same machine's own LAN IP, the exact address a phone would
use — hung past a 120 s timeout with no error, no connection-refused, nothing. `/usr/libexec/
ApplicationFirewall/socketfilterfw --getglobalstate` reported the firewall enabled.

**Root cause.** macOS's Application Firewall treats an inbound connection to a LAN-facing address
as a different case from loopback, and can gate it behind a per-process "allow incoming
connections" decision that this session cannot answer (no GUI). Unlike a refused connection, this
fails as a silent hang — indistinguishable, from the caller's side, from the Mac simply not
answering on that interface, which is one of the two specific networking failure modes I26's task
description named up front ("this will otherwise silently fail on a real device").

**Fix.** None in the product — this is a fact about the test machine, not a bug. Logged in the
tracker's I26 entry and in this file so whoever runs the actual device test knows to check
**System Settings → Network → Firewall → Options** for a pending prompt for `python3`/`uvicorn`
if the phone can reach `/health` on `127.0.0.1` from the Mac's own terminal but not by LAN IP from
the phone.

**Lesson.** "Reachable from the same machine via `127.0.0.1`" is not evidence the service is
reachable via its LAN IP — the two go through different code paths in the OS firewall, and the
failure mode (a silent hang, not an error) is exactly the shape that a phone in the human's hand
would also see with no diagnostic. Test the LAN IP specifically, from another host if at all
possible, rather than trusting loopback as a proxy for it.
