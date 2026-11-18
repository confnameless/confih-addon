# Confih Hi-Res — a BitChord addon

**Hi-res Qobuz and Tidal inside [BitChord](https://bitchord.kushagrasingh.in), plus the
public site that reports what it is doing.**

Paste one URL into BitChord and it starts returning 24-bit / 192 kHz FLAC, Dolby Atmos where
Tidal has it, CD lossless everywhere else. No ads, no accounts, no tracking, no third-party
scripts on the site.

Source: <https://github.com/confnameless/confih-addon> — the gateway behind the address below,
not a fork of the BitChord app itself.

### Live site

| | |
|---|---|
| **Addon + status** | <https://bitchord.confih.tech> |
| What is playing | <https://bitchord.confih.tech/plays> |
| How it works | <https://bitchord.confih.tech/about> |
| Support the server | <https://bitchord.confih.tech/support> |

## Add it to BitChord

1. Open **BitChord → Settings → Sources → Add an addon**.
2. Paste the URL — root or manifest, both work:

   ```
   https://bitchord.confih.tech
   ```

3. Run the health check BitChord offers, then play something.
4. In the addon's settings, pick **Audio quality** (defaults to *Hi-Res & Dolby Atmos*).

Requirements:

- **BitChord 1.9.1 or newer, official Android app.** The addon declares
  `"bitchordAuth": true`, so it only answers requests carrying the app's signed,
  hardware-backed proof. Forks, browsers, `curl` and scripts get `401` — that is deliberate.
- You still need **your own** Qobuz or Tidal subscription. This addon does not include,
  share or unlock anyone's account.

## Quality

BitChord sends a `quality` value on search and stream requests. The response is labelled from
what the provider actually returned, never from what was asked for.

| Setting | Requested | If the track is not hi-res |
|---|---|---|
| `max` *(default)* | 24/192 → 24/96 → 16/44.1 FLAC → MP3 320 | labelled honestly, e.g. `Lossless · 16-bit / 44.1 kHz` |
| `hires` | same ladder as `max` | same |
| `lossless` | 16/44.1 FLAC → MP3 320 | MP3 is labelled MP3 |
| `high` | MP3 320 | — |
| `low` | MP3 320 (the lowest both providers serve) | — |

A CD-only album requested at `max` says CD lossless. A track that fell back to MP3 says MP3.
No hi-res label ever lands on a file that is not hi-res.

## How it works

The addon resolves metadata; your phone downloads the audio straight from the provider.
Audio never transits this server, which is why one small box is enough.

```
BitChord ── manifest / search / stream (a few KB of JSON) ──▶  Confih addon
                                                                   │
                                                          Qobuz / Tidal API
                                                                   │
BitChord ◀──────────── FLAC / Atmos (direct CDN) ──────────────────┘
```

- **Qobuz first, Tidal fallback.** A track Qobuz cannot serve (catalogue gap, expired
  session, rate limit) is resolved from Tidal instead of failing, and the status page says so.
- **Honest metadata.** Codec, bit depth and sample rate come from the resolved file.
- **Dolby Atmos** comes through Tidal's E-AC-3 JOC renditions when the tier asks for it.
- **Cover art is proxied** through this host (`/art/…`), so the player makes no third-party
  image request.
- **Cached streams stay valid**: short-lived signed URLs are re-resolved rather than replayed.

## The public site

The same process serves a small, dependency-free site — no analytics, no CDN, no fonts fetched
from anywhere, CSP locked to `'self'`.

- **Status** — provider health, request reliability, 30-day uptime, incident log.
- **Plays** — ranked tracks and an anonymous recent feed, updated every 10 seconds.
- **About** — what the gateway does and does not do.
- **Support** — how the server is paid for, if you feel like it.

## Privacy — what is recorded

- **Search and stream requests**: counters only. Nothing is stored per listener.
- **The play ledger**: track id, title/artist/album, quality tier, minute-rounded timestamp,
  and a client key that is a **per-process salted hash of the IP, truncated to 12 hex
  characters**. It cannot be reversed, and it stops matching anything when the process
  restarts. It exists to count distinct listener sessions, not to follow them.
- **No** cookies, no fingerprinting, no ad or analytics network, no third-party beacon that
  survives the CSP.
- Qobuz and Tidal credentials live in the server's environment; they are never sent to
  clients, logged, or exposed by the public API.

## Roadmap

Qobuz and Tidal are the providers the gateway speaks today. The addon core is not built around
either of them — it is search → resolve → verify → return honest metadata — so adding a platform
means writing an adapter, not rewriting the addon.

Planned, in rough order of confidence. **None of this has shipped yet, and there are no dates.**

- [ ] **More providers.** Each one needs its own auth flow and signed-URL shape; the pool,
      failover, rate limiting and metadata labelling are already provider-agnostic.
- [ ] **Per-provider preference** in the addon settings, so a listener can pin the platform they
      pay for instead of taking the default order.
- [ ] **Deeper per-provider status** on the public site — one health line and one quality mix per
      platform rather than a single gateway view.
- [ ] **More than one client.** The protocol is a small JSON contract; other players that speak
      the same one should be able to use this gateway without a second deployment.

What will not change: no ads, no tracking, no account resale, and no quality claim that the
provider did not actually deliver.

## Self-hosting

Everything here is runnable: Docker or systemd + Caddy, a VPN sidecar for geo-restricted
catalogues, token-refresh handling, and the exact env vars.

→ **[docs/SELF-HOSTING.md](docs/SELF-HOSTING.md)**

Start from [`.env.example`](.env.example); it explains every value, including the ones that
must stay secret.

## Security

The repository ships no credentials: provider tokens, Tidal OAuth secrets, admin passwords and
the path secret are all environment-only. What is public here is the API surface, which is
discoverable with `curl` whether or not the source is published.

→ **[SECURITY.md](SECURITY.md)** for the threat model and how to report a problem.

## Not affiliated

Not affiliated with, endorsed by, or supported by Qobuz, Tidal, YouTube, BitChord or any
label. Provider names and marks belong to their owners. This service hosts no music: it
returns signed URLs from the provider you already pay for, to the client that asked.

If you are a rights holder with a concern about this deployment, open an issue in this
repository and it will be read.

## Support

The gateway is free to use and paid for out of one person's pocket. Donations cover compute,
egress and storage; they buy no priority, no perks and no extra quality.

- Addresses and terms: <https://bitchord.confih.tech/support>
- Bugs and provider breakage: [open an issue](https://github.com/confnameless/confih-addon/issues)
  with a track id and the quality you requested.
- Starring the repo is the cheapest way to help other BitChord users find this gateway.

## License

No license file is included yet: the source is published for transparency and as a
self-hosting reference. If you want others to reuse it, add one — MIT is a reasonable default
for a project like this.
