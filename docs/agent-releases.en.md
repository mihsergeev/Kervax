# Releasing and updating the Kervax agent

How to build, sign and roll out a new agent version by hand — with no external
services. It's two or three commands.

## In short: why this is safe

The root of trust is the **offline private key**
(`agent-signing/kervax-agent.key`).

- Agents install a new binary **only if** its manifest is signed with that key, the
  version is **strictly newer** than the current one (anti-rollback), and the
  **sha256 matches**.
- The private key **never** reaches the panel and is never committed to git.
- So even a fully compromised panel **cannot sign** its own binary and cannot push
  anything to the agents. The panel only serves already-signed bytes.

See [`docs/security.en.md`](security.en.md) for the threat model.

---

## One-time prerequisites

- **Python 3.11+** with the `cryptography` package (`pip install cryptography`).
- **Docker** on the machine that runs the release — for a reproducible build
  (build == sign, maximum trust). Or a remote build host over SSH (see below), but
  then it enters the trust chain.
- Everything else (`agent-signing/*.py`) is already in the repo.

---

## Step 0. Once: create the signing key

Only if you don't have a key yet (`agent-signing/kervax-agent.key` is missing).

```bash
python agent-signing/keygen.py          # key encrypted with a passphrase (recommended)
# or:  python agent-signing/keygen.py --plain   # no passphrase (insecure)
```

Produces:
- `agent-signing/kervax-agent.key` — the **private** key. Mode `0600`, in `.gitignore`.
  **Back up the key and its passphrase in a password manager** — lose them and you
  can't update agents anymore (you'd have to mint a new key and reinstall every agent).
- `agent-signing/kervax-agent.pub` — the **public** key. It gets baked into the
  agent binary at build time. It is deliberately absent upstream: every
  installation has its own, and an agent built with someone else's public key
  would trust someone else's signatures. Committing it to your own fork or
  deployment repository is fine — it is not a secret.

If you generated it `--plain`, encrypt it later:
```bash
python agent-signing/protect_key.py
```

> The public key is **baked into the binary automatically** at build time
> (`release.py` reads it from `kervax-agent.pub`). No need to edit `agent/main.go`.

---

## Step 1. Build and sign a new version

1. **Bump the version** in `agent/main.go`:
   ```go
   const version = "1.21"   // was 1.20
   ```

2. **Build and sign** (the number must match `main.go`):
   ```bash
   python agent-signing/release.py 1.21
   ```
   If the key is encrypted, it will ask for the passphrase.

What `release.py` does:
- builds static `amd64` and `arm64` binaries with the public key baked in
  (in Docker: `build == sign`);
- computes `sha256`/size, writes a canonical `manifest.json`, signs it with the
  offline key and **self-verifies** the signature;
- drops into `agent-dist/`: `manifest.json`, `manifest.sig`,
  `kervax-agent-amd64`, `kervax-agent-arm64`.

**Building without local Docker** (a remote build host enters the trust chain):
```bash
# or put the target in agent-signing/build-host.txt (e.g. root@203.0.113.10 or an ssh alias)
KERVAX_BUILD_SSH=my-build-host  python agent-signing/release.py 1.21
```
`release.py` warns that the build host is in the trust chain. For maximum trust, run
Docker locally.

---

## Step 2. Publish the artifacts to the panel

The panel serves the files from its `agent-dist/`
(`/api/agent/manifest`, `/api/agent/manifest.sig`, `/api/agent/download/<arch>`), so
the fresh `agent-dist/*` must land in the panel image.

If the panel runs in Docker (typical), redeploy it **with `agent-dist/`** and rebuild
the backend:

```bash
# from the repo root, on the machine that can reach the panel:
docker compose up -d --build backend
```

> Make sure the deploy **includes** `agent-dist/` and **excludes** secrets:
> `tar tzf <your_tarball> | grep -iE 'agent-signing|\.key$'` — must be **empty**.
> `agent-dist/` only holds signed binaries and the manifest — those are fine to ship.

Confirm the panel sees the new version:
```bash
curl -s https://YOUR-PANEL/api/agent/manifest        # version should be 1.21
```

> If the panel **builds** the agent binary itself (the build stage in
> `backend/Dockerfile`), set `KERVAX_AGENT_PUBKEY=<contents of kervax-agent.pub>` in
> `.env` so panel-built binaries carry your public key. `release.py` bakes it either
> way; the env var only matters for the panel-side build.

---

## Step 3. Roll out to agents

The rollout is only a "wish": the panel asks the agent to update, and the agent
itself verifies the signature/hash/anti-rollback and installs only a valid binary.

**Via the UI:** `Servers` → (optionally one node first — canary) → "Update all".

**Via the API:**
```bash
TOKEN=... # panel admin JWT
# all enabled agents:
curl -s -X POST https://YOUR-PANEL/api/servers/agent-update \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"version":"1.21"}'
# only selected (canary):
#   -d '{"version":"1.21","server_ids":[3]}'
```

Each agent picks up the "wish" on its next report (within the interval), downloads the
binary, verifies it and re-execs with the same PID.

---

## Step 4. Verify the rollout

```bash
# via API: check agent_version per server
curl -s https://YOUR-PANEL/api/servers -H "Authorization: Bearer $TOKEN" \
  | python -c 'import sys,json; [print(s["name"], s["agent_version"]) for s in json.load(sys.stdin)]'
```
Or on the node itself:
```bash
journalctl -u kervax-agent -n 5 --no-pager   # "kervax-agent 1.21 → https://…"
```

If an agent refused, the log says why: bad signature, not newer than current
(anti-rollback), or sha256 mismatch. That's the protection working, not a bug.

---

## Installing / reinstalling an agent on a server

First install (the panel gives you the command with a token:
`Servers → Add server`):
```bash
curl -fsSL https://YOUR-PANEL/api/agent/install.sh | sh -s -- https://YOUR-PANEL <TOKEN>
```
- Installs the binary into `/opt/kervax/bin` (owned by the unprivileged `kervax`
  user), the config into `/etc/kervax-agent.conf`, the systemd unit `kervax-agent`.
- A second agent on the same server (metrics to another panel) — a third argument:
  `… | sh -s -- https://PANEL2 <TOKEN2> panel2` → unit `kervax-agent@panel2`.

**Reinstall** is needed when the systemd unit itself changes (OTA updates only the
binary, not the unit). Just run the same install command again — the unit is rewritten
and the agent restarts. Reuse the token or rotate it in the panel.

---

## Security rules (short)

- The private key (`agent-signing/kervax-agent.key`) stays **offline**, passphrase-
  protected, backed up in a password manager. Never: in git, on a server, in a deploy
  tarball. Before every ship: `tar tzf … | grep -iE 'agent-signing|\.key$'` → empty.
- The public key (`kervax-agent.pub`) — not a secret, but it is deliberately
  absent upstream: each installation has its own, and an agent built with someone
  else's public key would trust someone else's signatures. Committing it to your
  own fork or deployment repository is fine.
- For maximum trust, build with local Docker (`build == sign`). If you build on a
  remote host, it's in the trust chain — keep it clean.
- Only bump the version — anti-rollback won't let you install an older one.

---

## If you lose the private key

You can no longer update existing agents (they trust the old public key). Recovery:
1. `keygen.py` — a new keypair (new `kervax-agent.pub`).
2. `release.py <version>` — build the agent with the new public key.
3. Publish to the panel.
4. **Reinstall** the agent on every node via the install command (old agents won't
   accept the new key — they need the new binary). After that, OTA works again.
