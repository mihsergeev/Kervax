# Kervax security model

The core guarantee: **the panel is only a metrics receiver.** Even if the panel is
fully compromised, an attacker gains no access to the servers — they can only spoil or
fake what the dashboard shows.

## 1. Outbound-only

The agent **listens to nothing** and **executes no commands**: it has no network
listener (`net.Listen`/HTTP server) and never spawns a shell or processes. It only
**POSTs metrics** to the panel over HTTPS periodically. The panel physically cannot
connect back to the server — there is no open port to reach.

## 2. The agent is unprivileged and locked down

- System user `kervax`: no home directory, shell `/usr/sbin/nologin`.
- systemd hardening: `NoNewPrivileges=true`, `ProtectSystem=strict`,
  `ProtectHome=true`, writes allowed **only** to `/opt/kervax-agent/bin`
  (`ReadWritePaths`). The single capability is `CAP_SYSLOG` (read-only on
  `/dev/kmsg`, to learn the name of an OOM-killed process).

## 2b. The panel itself does not run as root either

- The backend and the scheduler run as `kervax` (uid 10001). The container
  starts as root only long enough to fix ownership of the mounted volume and
  then drops privileges, so upgrading does not break a `./data` directory
  created by the previous, root-running image.
- The frontend is nginx built to run as uid 101 on port 8080 — the panel has no
  use for port 80, a reverse proxy publishes it.
- Every container runs with `no-new-privileges:true`.

## 3. What the panel can even tell the agent

The panel's reply to the agent is a struct with exactly two fields:

```go
type config struct {
    Interval int         // how often to send metrics
    Update   *updateWish // "update to version X" — a wish only
}
```

No "run", "read a file", "open a port". At most it can change the interval or **ask**
the agent to update.

## 4. Updates rely on a signature, not on trusting the panel

The agent installs a new binary **only if all three hold**:

1. **The manifest signature is valid** — Ed25519, verified with the **public key
   baked into the binary**. The private half is offline
   (`agent-signing/kervax-agent.key`), in `.gitignore`, **never** on the panel or in
   the deploy tarball.
2. **The version is strictly newer** than the current one — anti-rollback (you can't
   push back an older, possibly vulnerable version).
3. **sha256 and size** of the downloaded binary match the signed manifest.

The binary swap is atomic (`rename`); the restart is an `exec` of the same binary
(not an arbitrary command).

## 5. Threat model: the panel is compromised

An attacker with full control of the panel has: the public key (already public), the
agents' token hashes (only for authenticating the agent **to** the panel), and the
ability to serve any files. They **cannot**:

- forge the manifest signature — **no private key** → the agent rejects any binary
  they serve;
- roll an agent back to an older version (anti-rollback);
- run a command, open a shell, read files, or pivot onto the server — the agent
  simply can't do any of that.

Worst case: it lies in the UI about metrics, changes the interval, or asks to update
to an **already legitimately signed** version. Access to the server: zero.

## 6. The real trust boundary

- **The private key** is the root of trust. Keep it offline, passphrase-protected,
  backed up in a password manager. Before any ship:
  `tar tzf … | grep -iE 'agent-signing|\.key$'` → must be empty.
- **The build host** is in the trust chain: it produces the bytes you will sign. For
  maximum trust, build with local Docker (`build == sign`, reproducible — hashes match
  byte-for-byte). If you build on a remote host over SSH, keep it clean.

The release/signing procedure and key-loss recovery are in
[`docs/agent-releases.en.md`](agent-releases.en.md).
