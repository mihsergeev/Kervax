# Security Policy

Kervax holds tokens for every agent in a fleet and, if you use the backup vault,
the credentials that restore your data. Security reports are welcome.

## Reporting a vulnerability

Please report privately via GitHub Security Advisories ("Report a vulnerability"
on the repository's Security tab) rather than opening a public issue. Include
what you did, what happened, and the version (`⚙ → About` shows it). Reports are
usually acknowledged within a few days.

## Supported versions

The latest release is supported. Fixes go into a new release, not into patches
for old ones.

## What the design already assumes

The full threat model is in [docs/security.en.md](docs/security.en.md)
([по-русски](docs/security.md)); in short:

- **The panel never connects into your servers.** The agent has no listener, runs
  no shell, and only POSTs metrics outwards over HTTPS. A fully compromised panel
  can lie about what it shows — it cannot reach a node through the monitoring
  channel.
- **Agent tokens are stored as SHA-256.** A dump of the database does not let
  anyone impersonate a node.
- **Agent updates are gated by an offline key.** The panel serves the release but
  cannot sign it; an agent installs an update only if the signature verifies, the
  version is strictly newer, and the binary's hash matches the signed manifest.
- **Roles are enforced in the API.** An account limited to a group cannot reach
  objects outside it by guessing an id — `ops/selfcheck.py` and
  `ops/audit_live.py` exist to keep that true as the code changes.

## Notes for whoever deploys it

**Put TLS in front and restrict who can reach the panel.** It ships no TLS of its
own and binds to `127.0.0.1` by default. The bundled caddy overlay takes
`KERVAX_ALLOW_IPS`; agents need only the ingest endpoint, not the whole panel.

**The panel refuses weak secrets.** `KERVAX_JWT_SECRET`, `KERVAX_DB_PASSWORD` and
`KERVAX_ADMIN_PASSWORD` must be set to real values or it will not start. Change
the admin password after the first sign-in and turn on 2FA.

**Treat `editor` as trusted with network reach.** Anyone who can create a monitor
can point it at any address the panel can reach, including internal ones.
Response bodies never reach the UI — only the status code and whether a keyword
matched — but the reach itself is real.

**Configuration backups contain credentials.** The exported JSON and the
scheduled on-disk backups include alert-channel tokens and monitor
authentication. Keep them where you would keep a private key.

**The backup vault is the exception that is not.** Vault entries are encrypted in
the browser with a password the panel never receives, so a database dump does not
expose restore credentials — but a plain-text export of the vault does. It is
plain text on purpose; store it accordingly.
