# Security

This repository is the source of a small, single-purpose gateway. Publishing it is a
transparency choice, not a claim that reading it gives anyone access to the live service.

## What is deliberately **not** in this repository

- No Qobuz account token, password, email or user id.
- No Tidal OAuth secrets. The app *identifiers* are public; the secrets are read from the
  environment (`TIDAL_ATMOS_CLIENT_SECRET`, `TIDAL_FIRE_TV_CLIENT_SECRET`,
  `TIDAL_LEGACY_CLIENT_SECRET`).
- No admin/vault password, stats token, path secret or tunnel credentials.
- No analytics identifiers, no real listener data, no play ledger.

If you find any of the above in this repository or its history, treat it as a security report
(see below) rather than a curiosity.

## How the live service is protected

- **Addon auth.** The gateway declares `"bitchordAuth": true` and verifies BitChord's signed,
  hardware-backed proof on every search and stream request. Requests without it get `401`.
- **Admin plane.** `/internal/admin/*` refuses requests that arrive through Cloudflare and
  requires a stats token; the web vault is password-gated with a 20-attempts/minute limiter.
- **Rate limiting** per client, plus provider-side concurrency caps.
- **Strict CSP** (`default-src 'self'`, no third-party origins) and no third-party scripts.
- **No user database.** There is nothing to dump: no accounts, no emails, no sessions.
- Secrets live in the deployment environment only, never in the image or the repo.

## What a reader of this source can learn

Which endpoints exist and how they behave — that is public API surface by design, and
discovering it with `curl` takes seconds regardless of what is published. Source disclosure
does not hand out credentials; the tokens, the admin password and the path secret are the
things that would, and none of them are here.

## Reporting a vulnerability

Open an issue marked **security** with the smallest reproduction you can, or a description of
the class of bug if you would rather not publish the details. Please do not test against other
listeners' traffic or attempt to access accounts that are not yours.

Reports that matter most: auth bypass on `/search` or `/stream/*`, admin/vault access, SSRF
through the artwork or provider proxy paths, path traversal in the artwork cache, and anything
that exposes provider credentials or listener data.

## Not in scope

- Qobuz or Tidal enforcing their own terms. That is between the operator and the provider.
- Denial of service by volume.
- Findings that require a compromised host, a leaked environment file, or physical access.
