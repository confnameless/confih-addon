# BitChord Qobuz addon

A small HTTP addon that lets [BitChord](https://bitchord.kushagrasingh.in/docs)
search Qobuz and play its lossless / hi-res FLAC streams. It implements the
three endpoints BitChord's addon protocol asks for:

```
GET /manifest.json
GET /search?q=…&quality=…
GET /stream/{track_id}?quality=…
```

An optional secret path prefix moves all three under `/s/<secret>/…`; an
optional BitChord-auth mode restricts them to the official Android app.

## Why this is enough on a 2 GB / 5 MB/s server

The addon never proxies audio. It asks Qobuz's JSON API for a signed CDN URL
and returns that URL to the phone, which downloads the FLAC directly from
Qobuz:

```
BitChord  ── manifest / search / stream (JSON, a few KB) ──▶  this addon
    │                                                            │
    │                                                     Qobuz JSON API
    │                                                            │
    └──────────── FLAC / MP3 (audio) ────────────────▶  Qobuz CDN
```

The server only carries metadata, so a 1 vCPU / 2 GB box is more than enough
for personal use, and 5 MB/s of server bandwidth is irrelevant. The 2 GB Indian
server is enough even with the Proton VPN sidecar; the 24 GB Ampere machine is
not needed for this workload.

## Requirements

- An active **Qobuz Studio** subscription (hi-res included) created in a
  country Qobuz serves.
- A server with a stable public HTTPS hostname, **or** an existing server plus
  the free Proton VPN egress described below. Docker is the easiest route;
  plain systemd + Caddy also works.
- The official BitChord Android app. This deployment enables addon auth, which
  requires BitChord 1.9.1 or newer.

### Important: country and account status

Qobuz's **catalogue API** is geo-restricted. From an unsupported country
(India, for example) `track/search` returns zero tracks while artists and
playlists may still answer. The Qobuz **audio CDN** is not blocked and signed
URLs play fine from India, so only JSON API calls need a supported-country
egress. A VPN on the phone does **not** help: Qobuz checks the IP of the server
running the addon.

Choose one:

1. Run the addon on a VPS in France, Germany, the Netherlands, the UK or the US;
2. Keep your existing Indian server and put the addon behind **Proton VPN
   Free** with the included `docker-compose.vpn.yml` (free, no new server, no
   card); or
3. Set `QOBUZ_PROXY_URL` to an HTTP/SOCKS proxy in one of those countries. Only
   the small API requests use the proxy; audio still goes phone → Qobuz CDN.

A `QOBUZ_USER_AUTH_TOKEN` is a password-equivalent credential, not a
subscription. The addon can refresh an expiring token only when
`QOBUZ_EMAIL` and `QOBUZ_PASSWORD` are also set; a token-only setup works
until that token expires, then it must be replaced. Without a valid token,
Qobuz returns `401` or 30-second samples and the addon reports `qobuz_auth`.

### Qobuz vs Tidal

Neither service sells subscriptions in India, but they behave differently in
practice. Qobuz's catalogue API returns zero tracks from an Indian IP, so
Qobuz definitely needs a supported-country egress. Tidal's API edge answers
from India — a plain `401` means missing authentication, not a geo-block — and
an existing subscription may stream without a VPN. That is worth testing if
you already have Tidal.

| | Qobuz | Tidal |
|---|---|---|
| Sold in India | No | No |
| From an Indian IP | Catalogue returns 0 tracks; egress required | API reachable; playback needs an account test |
| Individual price | $12.99/month (or $129.99/year) | $11.99/month |
| Hi-res FLAC | Yes, direct signed file URL | Yes, but usually a DASH manifest |
| Dolby Atmos | No | Yes |
| Official API playback | Partner-only | Only through the official Player SDK; third-party apps get previews |
| Self-hosted addon path | This repo | Unofficial web API, with more auth/manifest churn |

BitChord already plays Tidal's DASH/FLAC and Atmos responses, so the client is
not the blocker. The blocker is Tidal's API: the documented API exposes the
catalogue but not full playback, and community tools that scrape the web
player broke repeatedly in 2026. If you already pay for Tidal, test playback
before building anything; otherwise Qobuz remains the simpler target because
this addon already works.

### Free option: Proton VPN Free on an existing Indian server

You do **not** need a foreign VPS to start. Proton VPN Free has no data cap and
its free plan can generate WireGuard configuration files.
`docker-compose.vpn.yml` runs gluetun as a sidecar and gives **only the addon**
the VPN's network namespace:

```
BitChord ── HTTPS ──▶ Caddy (public, India) ──▶ addon (VPN namespace)
                                                   │
                                          Proton VPN Free (NL/US)
                                                   │
                                                   ▼
                                            Qobuz JSON API

BitChord ───────────── audio ────────────────────▶ Qobuz CDN (direct)
```

Only manifest/search/stream-resolution/health JSON crosses the VPN. The FLAC
still goes straight from Qobuz's CDN to the phone, so Proton's free speed limit
is irrelevant. Caddy is **not** behind the VPN: Proton Free has no inbound port
forwarding, and inbound HTTPS must arrive on the server's real IP.

Trade-off: free Proton exits are shared IPs. Qobuz may block them, just as it
blocks Indian IPs. Test with `scripts/qobuz_check.py`; if the exit is blocked,
try the other country (`PROTON_SERVER_COUNTRIES=United States` or
`Netherlands`), then fall back to a free cloud VM. Proton Free also allows only
one device connection at a time, so don't use the same account on your phone
while the server's gluetun is connected.

#### 1. Create the Proton WireGuard key

1. Sign up at <https://account.protonvpn.com> (free plan, no card).
2. Go to **Downloads → WireGuard configuration**.
3. Create a config for a free server in the **Netherlands** or **United
   States**. Prefer the country that matches your Qobuz account's signup
   country (US account → United States, EU account → Netherlands). On the free
   plan the only VPN option offered is VPN Accelerator; that is fine.
4. Open the downloaded `.conf` file and copy only the `PrivateKey = …` value.
   Do not commit the file. Proton keys are valid for about a year; regenerate
   the config if gluetun suddenly stops authenticating.

#### Test the key on your PC without routing the whole machine

Do this before touching the server. `scripts/proton_isolated_test.sh` runs
`wireproxy` in user space: it opens the WireGuard tunnel and exposes a local
SOCKS5 proxy. Only the test process uses it. No interface, route, DNS or
firewall setting on the host is changed, and the tunnel is killed when the
script exits. It does not need root.

Save the Proton `.conf` outside the repository and keep it private:

```bash
install -m 600 ~/Downloads/proton.conf /tmp/opencode/proton.conf
scripts/proton_isolated_test.sh /tmp/opencode/proton.conf
```

The script prints the direct IP and the proxied IP. The proxied one must be
different and in the country you picked (Netherlands or the United States on
the free plan). If Proton's desktop app is connected, disconnect it first:
the free plan allows one device connection at a time.

To check that Qobuz's catalogue works from the VPN exit, without needing a
subscription:

```bash
scripts/proton_isolated_test.sh --geo /tmp/opencode/proton.conf
```

To run the full Qobuz check through the isolated tunnel as well:

```bash
scripts/proton_isolated_test.sh --qobuz /tmp/opencode/proton.conf "daft punk"
```

The Qobuz checks need the project's Python dependencies (`pip install -r
requirements.txt`); the script uses `.venv/bin/python` when it exists.
`--qobuz` also needs a valid Qobuz token in `.env`. Its CDN probe goes through
the proxy for that one request; production audio does not.

Once this passes, put only the `PrivateKey` value in the server's `.env`. The
complete `.conf` file never needs to leave your PC.

#### 2. Point `bitchord.confih.tech` at the Indian server

At the Orderbox DNS panel for `confih.tech`, add:

| Type | Host | Value | TTL |
|---|---|---|---|
| `A` | `bitchord` | the server's public IPv4 | 300 (or default) |

Do not add an `AAAA` record unless the server has working public IPv6; a stale
AAAA record makes Caddy's certificate request fail. Verify before starting:

```bash
dig +short bitchord.confih.tech
```

If the server has no public IPv4 — for example an Oracle Cloud instance
reachable only over Tailscale — an A record cannot work. Use the outbound
tunnel path below instead of Caddy.

### Server without a public IPv4 (tunnel mode)

A small cloud instance behind Tailscale (or any shared NAT egress) is this
case: no route out of the provider's NAT reaches ports 22/80/443, so DNS cannot
point at the instance.

Run gluetun and the addon without Caddy, publishing the addon on localhost
only:

```bash
docker compose -f docker-compose.vpn.yml -f docker-compose.tunnel.yml \
  up -d --build vpn addon
```

Then expose it with one of these:

- **Cloudflare Tunnel (recommended, custom hostname).**

  This deployment uses a locally-managed tunnel, so the Zero Trust dashboard
  (which may ask for a payment method) is not involved. `confih.tech` is
  already on Cloudflare and the public hostname is `bitchord.confih.tech`.

  1. On a machine with a browser, authorize the zone once:

     ```bash
     cloudflared tunnel login
     ```

     Select `confih.tech` and click **Authorize**. This writes
     `~/.cloudflared/cert.pem`; no card is required.
  2. Create the tunnel and its DNS route:

     ```bash
     cloudflared tunnel create bitchord-local
     cloudflared tunnel route dns <tunnel-id> bitchord.confih.tech
     ```

  3. Put the credentials and ingress on the server. The credentials JSON is
     `~/.cloudflared/<tunnel-id>.json`; copy it to
     `cloudflared/<tunnel-id>.json` on the server (mode 400). Then write
     `cloudflared/config.yml`:

     ```yaml
     tunnel: <tunnel-id>
     credentials-file: /etc/cloudflared/<tunnel-id>.json

     ingress:
       - hostname: bitchord.confih.tech
         service: http://vpn:8000
       - service: http_status:404
     ```

     `vpn` is the gluetun container, which shares its network namespace with
     the addon. The whole `cloudflared/` directory is ignored by git and rsync
     because it contains the tunnel secret.
  4. Set `CLOUDFLARED_UID` and `CLOUDFLARED_GID` in `.env` to the UID/GID that
     owns `cloudflared/` so the connector can read the mode-400 credentials
     while still running as a non-root user. On the Oracle server `ubuntu` is
     `1001:1001`.
  5. Start the connector:

     ```bash
     docker compose -f docker-compose.vpn.yml -f docker-compose.tunnel.yml \
       --profile cloudflare up -d tunnel
     ```

  6. Verify `https://bitchord.confih.tech/healthz`. Cloudflare terminates TLS
     and sends `X-Forwarded-For`; the compose file already sets
     `TRUST_PROXY_HEADERS=true`.

  Cloudflare's free plan is sufficient: only small JSON responses pass through
  the tunnel. Audio goes from the Qobuz CDN straight to each phone.
- **Cloudflare quick tunnel (temporary, no account).**
  `cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate` prints a
  random `https://*.trycloudflare.com` URL. Good for a phone test, not for
  production: the URL changes on restart and has no uptime guarantee.
- **Tailscale Serve/Funnel.** `tailscale serve --bg 8000` gives
  `https://main.<tailnet>.ts.net`; Funnel makes it public. This does not give
  you the custom hostname.

#### Capacity and beta monitoring

The addon never proxies audio. `/stream` returns a signed Qobuz CDN URL, so the
A1 instance handles only a few KB of JSON per play and each phone downloads the
FLAC directly from Qobuz. Measured against the live account from the Indian
server:

| Load | Result |
|---|---|
| 40 unique cold tracks at once | 40/40 HTTP 200 in ~7.6 s; all CDN probes 206 |
| 100 unique cold tracks at once | 100/100 HTTP 200 in ~20 s; p95 ~19 s |
| Repeated tracks/queries | served from cache, effectively instant |

BitChord's addon client has a 45-second call timeout, so even the 100-track
cold burst completes. Normal listening is staggered and mostly cache hits.

What can become a bottleneck:

- **One Qobuz subscription.** Qobuz's terms say access is strictly personal and
  cannot be used on several devices at the same time; Duo allows 2 and Family 6.
  Even 20–50 simultaneous listeners is outside that, so a beta with real users
  is at risk of Qobuz limiting or terminating the account. This is the real
  ceiling; the addon cannot work around it. For a public service, use a
  licensed source.
- **Qobuz API rate.** Each play start costs one `track/getFileUrl` call. The
  defaults are tuned for bursts (`QOBUZ_RATE_LIMIT_PER_SECOND=10`,
  `QOBUZ_RATE_LIMIT_BURST=20`, `QOBUZ_MAX_CONCURRENCY=16`); raise them only if
  Qobuz is not answering 429.
- **Caching.** Search results are cached for 10 minutes and signed stream URLs
  for 5 minutes (`CACHE_MAX_ENTRIES=4096`). Popular tracks do not hit Qobuz
  again while cached.
- **CGNAT.** Many Indian mobile users can share one public IPv4. The per-client
  limit is 600 requests/minute (`RATE_LIMIT_PER_MINUTE`) rather than 120 so a
  shared address is not throttled during a burst.

For real usage numbers, the addon writes a one-line summary every 60 seconds
(`STATS_LOG_INTERVAL_SECONDS`):

```bash
docker compose -f docker-compose.vpn.yml -f docker-compose.tunnel.yml \
  logs -f addon | grep traffic
```

Set `STATS_TOKEN` to a 16–128 character secret to enable `GET /stats`. It
requires the same value in an `X-Stats-Token` header:

```bash
TOKEN=$(grep '^STATS_TOKEN=' .env | cut -d= -f2)
curl -s -H "X-Stats-Token: $TOKEN" https://bitchord.confih.tech/stats \
  | python3 -m json.tool
```

It reports requests by endpoint, status codes, unique client IPs, peak
in-flight requests, cache hits/misses, client rate limits and Qobuz errors.
Cloudflare's dashboard also shows requests, unique visitors, countries and
bandwidth for the tunnel.

This deployment has BitChord auth enabled (`ADDON_AUTH_ENABLED=true` and
`ADDON_HOSTS=bitchord.confih.tech`). The manifest declares
`"bitchordAuth": true`, so BitChord no longer marks the addon as outdated, and
only the official BitChord app can use it. Auth does not remove Qobuz's
personal-use limit: the Qobuz account is still shared by every listener.

If you have OCI console access, the alternative is to give the instance a
public IPv4 (only possible in a public subnet) and open TCP 80/443 in the
security list. Then use the normal Caddy path and the Orderbox A record.

#### 3. Configure and start

In `.env` set:

```dotenv
ADDON_DOMAIN=bitchord.confih.tech
PUBLIC_BASE_URL=https://bitchord.confih.tech
ADDON_HOSTS=bitchord.confih.tech

PROTON_WG_PRIVATE_KEY=<PrivateKey from the Proton .conf file>
PROTON_SERVER_COUNTRIES=United States
# Pin the exit Qobuz accepted. Some Proton exits answer Qobuz with HTTP 403.
PROTON_SERVER_HOSTNAMES=node-us-408.protonvpn.net
PROTON_FREE_ONLY=off
```

`node-us-408` (US-MA#101, Boston) is the server verified from this Indian
connection. If Proton retires it, remove `PROTON_SERVER_HOSTNAMES` and let
gluetun pick another US server, or pin a different city/hostname.

Leave `QOBUZ_PROXY_URL` empty: the whole addon container already uses the VPN.
Then:

```bash
docker compose -f docker-compose.vpn.yml up -d --build
docker compose -f docker-compose.vpn.yml logs -f vpn addon
```

Do not run `docker-compose.yml` at the same time; both stacks publish 80/443.

#### 4. Verify the exit country

```bash
# Should print a Dutch/US IP, not the Indian server IP.
docker compose -f docker-compose.vpn.yml exec vpn wget -qO- https://ipinfo.io/ip

# With a valid Qobuz token, should report "geo_ok": true.
curl -s https://bitchord.confih.tech/healthz | python3 -m json.tool

# Run the full Qobuz check inside the VPN namespace, not on the Indian host.
docker compose -f docker-compose.vpn.yml exec addon \
  python scripts/qobuz_check.py --query "daft punk" --quality max
```

If `geo_ok` is false or the check reports zero tracks, the Proton IP is blocked
or the VPN is not connected. Check `docker compose ... logs vpn`, then try the
other country.

### If Proton VPN is blocked: free fallbacks

| Option | Cost | What it gives you |
|---|---|---|
| **Proton VPN Free + gluetun** (this repo) | Free | First thing to try. No new server, no card. Free exits are in NL/US/JP/PL/RO; use NL or US. |
| **Google Cloud Always Free e2-micro** | Free | One VM in `us-west1`, `us-central1` or `us-east1`; 30 GB disk, 1 GB egress/month. Only metadata and JSON responses leave the VM, so 1 GB is plenty for personal use. Card required; set a budget alert. |
| **Oracle Cloud Always Free** | Free | Always Free exists only in the account's **home region**, which cannot be changed. Choose a supported country (e.g. Germany, the Netherlands or the US) at signup; an existing India-home account will not help. Signup and capacity are unreliable. |
| **Paid VPS in FR/DE/NL/UK/US** | ~€4–5/month | The most predictable option. A 1 vCPU / 1 GB box is enough because audio is never proxied. |
| **Free public HTTP/SOCKS proxies** | Free | Do not use. They are unreliable and can see your Qobuz token and search metadata. |

Vercel Hobby and Koyeb free can also run the addon in a supported region, but
both need a serverless entrypoint or sleep/scale-to-zero handling. They are more
work than the Proton sidecar and are not wired up in this repo.

You still need a Qobuz Studio subscription created in a supported country.
Qobuz does not sell Studio in India. A VPN changes the egress IP; it does not
create an entitlement.

### DNS for `bitchord.confih.tech`

The domain already exists at Orderbox. Add one `A` record for the `bitchord`
subdomain and point it at the server's public IPv4. Do not use a wildcard and
do not add an AAAA record unless the server really has public IPv6. If you put
Cloudflare in front later, use **DNS only** (grey cloud) for the first
certificate issuance, or use a Cloudflare Tunnel if the origin has no public IP.

`ADDON_DOMAIN`, `PUBLIC_BASE_URL` and `ADDON_HOSTS` must all use the same
hostname: `bitchord.confih.tech`.

## Deploy from your laptop

opencode edits this folder on your PC; the server only needs the files and
Docker. There is no need to install an editor, Python or the source on the
server. From this directory:

```bash
rsync -av \
  --exclude .env --exclude data --exclude .venv \
  --exclude .pytest_cache --exclude .ruff_cache \
  ./ user@SERVER:/opt/bitchord-qobuz-addon/
```

Then create the server's `.env` once and start the stack:

```bash
ssh user@SERVER
cd /opt/bitchord-qobuz-addon
cp -n .env.example .env
chmod 600 .env
nano .env                 # fill in the values from the Quick start below
docker compose -f docker-compose.vpn.yml up -d --build
```

If `.env` is already filled in on your PC, copy it instead of retyping the
secrets:

```bash
scp .env user@SERVER:/opt/bitchord-qobuz-addon/.env
ssh user@SERVER chmod 600 /opt/bitchord-qobuz-addon/.env
```

The server needs Docker Engine with the Compose plugin, a public IPv4, TCP
80/443 open, and `/dev/net/tun` for the Proton sidecar. The Dockerfile installs
the Python dependencies inside the image, so the host does not need them.

Re-run the `rsync` and `docker compose ... up -d --build` commands after code
changes. The named volumes keep `data/qobuz-token.json` and Caddy's
certificates across rebuilds.

## Quick start with Docker

1. Point the `bitchord.confih.tech` `A` record at the server (see above) and open
   TCP 80 and 443; Caddy needs them for the certificate.
2. Copy the environment file and edit it:

   ```bash
   cp .env.example .env
   chmod 600 .env
   $EDITOR .env
   ```

   At minimum set:

   ```dotenv
   ADDON_DOMAIN=bitchord.confih.tech
   PUBLIC_BASE_URL=https://bitchord.confih.tech
   ADDON_HOSTS=bitchord.confih.tech
   QOBUZ_APP_ID=312369995
   QOBUZ_APP_SECRET=<the app secret from .env>
   QOBUZ_USER_AUTH_TOKEN=<fresh token>
   QOBUZ_USER_ID=<your numeric user id>
   ```

   `PUBLIC_BASE_URL` is only used to derive the hostname for addon auth; if you
   set `ADDON_HOSTS` explicitly it wins. Never commit `.env`.

3. Get a fresh Qobuz token. Either use the login helper:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python scripts/qobuz_login.py --update-env
   ```

   …or open `https://play.qobuz.com` in a browser, log in, then DevTools →
   Application → Local Storage → `https://play.qobuz.com` → key `localuser`
   → copy `token` into `QOBUZ_USER_AUTH_TOKEN` and `id` into
   `QOBUZ_USER_ID`.

   If the password is in `.env`, the addon can refresh an expired token by
   itself. With only a browser token, re-run the script when it expires.

4. Start the stack. On a server in a supported country:

   ```bash
   docker compose up -d --build
   docker compose logs -f addon
   ```

   On an Indian server, use the free Proton egress instead:

   ```bash
   # Fill PROTON_WG_PRIVATE_KEY and PROTON_SERVER_COUNTRIES in .env first.
   docker compose -f docker-compose.vpn.yml up -d --build
   docker compose -f docker-compose.vpn.yml logs -f vpn addon
   ```

   Run one stack or the other, never both: they both publish ports 80/443.

5. Check the server before involving the phone:

   ```bash
   curl -s https://bitchord.confih.tech/healthz | python3 -m json.tool
   ```

   With the direct stack, run the full check on the host:

   ```bash
   .venv/bin/python scripts/qobuz_check.py --query "daft punk" --quality max
   ```

   With the VPN stack, run it **inside the addon container** so it uses the
   Proton exit, not the Indian host IP:

   ```bash
   docker compose -f docker-compose.vpn.yml exec addon \
     python scripts/qobuz_check.py --query "daft punk" --quality max
   ```

   `qobuz_check.py` performs the same login, search, signature and CDN range
   request the addon uses, and tells you whether the account, the geo egress,
   the app secret and the actual FLAC bytes all work. `geo_ok: true` in
   `/healthz` only proves the addon's own egress is working.

6. In BitChord: **Settings → Sources → Add addon** and paste the base URL:

   ```
   https://bitchord.confih.tech
   ```

   If you set `ADDON_PATH_SECRET`, paste the prefixed URL instead:

   ```
   https://bitchord.confih.tech/s/<secret>
   ```

   Do not paste the `/manifest.json` suffix; BitChord accepts it, but the base
   URL is cleaner.

7. Play a track. The addon's **Audio quality** picker appears in the addon
   source settings. `Max` is the default and asks Qobuz for the best rendition
   it will give.

## Quality values and honest labels

BitChord sends `quality` on both search and stream requests. The addon declares
these values, best first:

| Value | What is requested from Qobuz | What the stream response says |
|---|---|---|
| `max` (default) | 24/192 FLAC → 24/96 → 16/44.1 → MP3 320 | The format Qobuz actually returned |
| `hires` | same as `max` | actual |
| `lossless` | 16/44.1 FLAC → MP3 320 | actual |
| `high` | MP3 320 | actual |
| `low` | MP3 320 (Qobuz's lowest) | actual |

Qobuz format ids are 27 (up to 24/192 FLAC), 7 (up to 24/96 FLAC), 6 (16/44.1
FLAC) and 5 (MP3 320). The response is labelled from the returned `format_id`,
`bit_depth` and `sampling_rate`, not from what was asked for. A CD-only track
requested at `max` is therefore reported as `Lossless · 16-bit / 44.1 kHz`, and
a track that falls back to MP3 is reported as MP3. That is deliberate: no
hi-res label on a file that is not hi-res.

Search rows are labelled the same way for the selected tier: at the `lossless`
tier a hi-res-capable recording still shows `LOSSLESS`, because that is what
this request will receive.

The addon also refuses Qobuz's silent failure mode: when the account is
unauthenticated or unentitled, `track/getFileUrl` returns HTTP 200 with a
30-second sample and a `restrictions` array. The addon detects the sample flag,
the `range=20-30` URL and the restriction codes, and returns a `503` with code
`qobuz_auth` instead of a sample.

## Deploying with systemd + Caddy

Docker is optional. On a Debian/Ubuntu host:

```bash
sudo useradd --system --home /opt/bitchord-qobuz-addon \
  --shell /usr/sbin/nologin bitchord-qobuz
sudo mkdir -p /opt/bitchord-qobuz-addon
sudo rsync -a ./ /opt/bitchord-qobuz-addon/
sudo chown -R bitchord-qobuz:bitchord-qobuz /opt/bitchord-qobuz-addon

cd /opt/bitchord-qobuz-addon
sudo -u bitchord-qobuz python3 -m venv .venv
sudo -u bitchord-qobuz .venv/bin/pip install -r requirements.txt
sudo -u bitchord-qobuz chmod 600 .env
sudo -u bitchord-qobuz mkdir -p data

sudo cp deploy/bitchord-qobuz.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bitchord-qobuz
sudo systemctl status bitchord-qobuz
```

The unit listens on `127.0.0.1:8000`. Put Caddy in front:

```caddyfile
bitchord.confih.tech {
    reverse_proxy 127.0.0.1:8000
}
```

Then set `TRUST_PROXY_HEADERS=true` in `.env` so per-client rate limiting reads
`X-Forwarded-For` instead of seeing every request as Caddy's local address.

The systemd route does not include gluetun. If the host is in India, either use
the Docker `docker-compose.vpn.yml` stack instead, or run a supported-country
proxy separately and set `QOBUZ_PROXY_URL` in `.env`.

## Enabling BitChord auth (recommended)

The addon verifies BitChord's signed request token and per-device proof using
the official verifier vendored in `app/vendor/`. It requires the official app
and HTTPS; forks, scripts and browsers are refused.

```dotenv
ADDON_DOMAIN=bitchord.confih.tech
PUBLIC_BASE_URL=https://bitchord.confih.tech
ADDON_AUTH_ENABLED=true
ADDON_HOSTS=bitchord.confih.tech
```

- `ADDON_HOSTS` is a comma-separated hostname list, without scheme or port. It
  must match the hostname you paste into BitChord exactly. `PUBLIC_BASE_URL`
  supplies it when `ADDON_HOSTS` is empty.
- The manifest then declares `"bitchordAuth": true`. BitChord learns auth is
  required from that flag, or from a `401` with
  `WWW-Authenticate: BitChord`, and retries with credentials.
- BitChord's auth retry only works over HTTPS (or loopback during development).
- Rotate the Qobuz token/password; they were shared in chat. Treat
  `QOBUZ_USER_AUTH_TOKEN` as a password.
- `ADDON_PATH_SECRET` is an additional URL-obscurity layer, not authentication.
  It must be 16–128 characters of `A-Z a-z 0-9 _ -`. Generate one with
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Do not expose port 8000 publicly. The Docker setup only publishes Caddy's
  80/443; the systemd unit binds loopback.

## Health and troubleshooting

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | Always 200 while the process is alive. Reports `status`, `token_ok`, `geo_ok`, `proxy_enabled` (only reflects `QOBUZ_PROXY_URL`, not the VPN sidecar), subscription hint and cache sizes. |
| `GET /readyz` | 200 when Qobuz is configured and not known-bad; 503 otherwise. |

Both are available at the root and under the secret prefix (so the container
health check can find them). They contain no credentials.

| Symptom | Cause / fix |
|---|---|
| `404` from the addon URL | Wrong base URL or wrong/missing `ADDON_PATH_SECRET`. Check `https://host/manifest.json` or `https://host/s/<secret>/manifest.json`. |
| Search returns no rows, artists/playlists work | The server's IP is in an unsupported country. Use `docker-compose.vpn.yml`, move to FR/DE/NL/UK/US, or set `QOBUZ_PROXY_URL`. Run `scripts/qobuz_check.py` from the same egress as the addon. |
| `geo_ok` is false with the VPN stack | The Proton exit is blocked or gluetun is not connected. Check `docker compose -f docker-compose.vpn.yml logs vpn`, try `PROTON_SERVER_COUNTRIES=United States`, or use a free cloud VM. |
| Caddy returns `502` with the VPN stack | The addon is waiting for a healthy VPN, or gluetun's firewall is not allowing port 8000. Check `docker compose -f docker-compose.vpn.yml ps` and `logs vpn addon`; keep `FIREWALL_INPUT_PORTS=8000`. |
| `vpn` container restarts or never becomes healthy | Wrong `PROTON_WG_PRIVATE_KEY`, `/dev/net/tun` missing, the host kernel has no WireGuard/TUN support, or the ISP blocks the WireGuard endpoint. Check `docker compose -f docker-compose.vpn.yml logs vpn`; enable TUN, try another network, or switch the sidecar to Proton OpenVPN over TCP 443. |
| `503 qobuz_auth`, or only 30-second samples | Qobuz token expired, password changed, or subscription ended. Re-run the login helper; renew Studio. |
| `503 qobuz_app_secret` / "Invalid Request Signature" | Qobuz rotated the web app secret. Run `scripts/qobuz_app_credentials.py` and update `QOBUZ_APP_ID`/`QOBUZ_APP_SECRET`. |
| `503 qobuz_config` | Missing app id/secret/token. Fill `.env`. |
| `429` with `Retry-After` | Client rate limit (`RATE_LIMIT_PER_MINUTE`, default 600) or Qobuz's own limit. BitChord waits and retries. |
| BitChord says the addon didn't ask for auth but requests fail `401` | `ADDON_HOSTS` does not match the pasted hostname, or the app is older than 1.9.1. |
| Caddy cannot get a certificate | DNS does not point at the server, ports 80/443 are closed, or Cloudflare is proxying during issuance. Use DNS-only initially. |
| Hi-res track plays as CD | The track is only available in CD quality, or the subscription/tier does not include hi-res. The response label states what was actually returned. |
| Server restarts lose the Qobuz token | `DATA_DIR` is not persistent. In Docker keep the `addon_data` volume; with systemd keep `data/` writable. |

Logs are redacted: the Qobuz token, app secret, password and path secret are
replaced with `***`, and query parameters such as `user_auth_token` are
scrubbed. Uvicorn access logging is disabled because the URL path may contain
the path secret. The bundled Caddy config also discards access logs for the
same reason; if you enable proxy access logging, it will record the secret
path, so prefer `ADDON_AUTH_ENABLED` over `ADDON_PATH_SECRET` there.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check app scripts tests
.venv/bin/pytest -q
```

Run locally against a real account:

```bash
.venv/bin/uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/manifest.json | python3 -m json.tool
```

The test suite uses `httpx.MockTransport`; it never contacts Qobuz and needs no
credentials.

## Notes and limitations

- This is an unofficial client of Qobuz's private JSON API. Qobuz can change it
  at any time. Use it for your own subscription and respect Qobuz's terms.
- No DRM is bypassed; the addon only asks Qobuz for URLs the account is
  entitled to.
- Search returns tracks only, not albums, artists or playlists. That is the
  part of the catalogue BitChord's addon protocol consumes.
- Downloads are performed by BitChord directly from the Qobuz CDN; the addon
  stores no audio. `ALLOW_DOWNLOADS=0` tells BitChord to stream only.
- The server is a coordinator, not a media proxy: if the phone cannot reach
  Qobuz's CDN, playback fails even when the addon's API calls work.
