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

## 2c. Root helpers: doing what the agent cannot

Some data is out of reach for an unprivileged agent by design — cluster PKI
expiry lives under root, and no sane RBAC hands out Flux secrets. Those jobs are
done by separate scripts (`agent/*-setup.sh`) that you install by hand and on
purpose; the agent neither runs them nor can run them.

- They live in `/lib65/kervax`, run from a systemd timer, and write an already
  parsed result to `/var/lib/kervax/*.json` (0644). The agent only reads it.
- The panel cannot command them: its reply to an agent contains nothing but an
  interval and a wish to update (see section 3).
- What leaves the node is conclusions, not raw material: `kubeexpiry-setup`
  sends a kind, a location, a date and a `Ready` state — no key material, no
  token values, not even their length.

About the network request. A token's expiry is not stored inside the token —
only the forge knows it — so `kubeexpiry-setup` asks **the host named in the
Flux source URL** (a self-hosted GitLab asks itself, not gitlab.com). The token
is used for that single request, is never written anywhere, and never leaves the
node. If you would rather not have that request: do not install the helper —
without it the panel simply does not know the dates, everything else is
unchanged.

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

## 3b. The agent probing a site: why this is not a hole

A site closed behind an IP allow-list cannot be checked by the panel — from
outside the connection is simply dropped. The agent on that very server does it
instead, and the panel tells it "probe this address". That sounds exactly like
the command section 3 says does not exist, so it matters what limits it.

**The agent only ever connects to localhost.** It substitutes the address
itself; from the URL it takes the name (Host and SNI), the scheme, the port and
the path. The panel cannot send it to any other host — not to a neighbouring
server, not into the internal network. A compromised panel does not turn a fleet
of agents into a scanner: the most it learns is how this one server answers,
which it already hears from that server anyway.

**What leaves the node is facts, not the page.** Status code, latency, error
text and whether a keyword was found — the agent does the keyword search itself.
The content of closed pages never reaches the panel, is not stored and is not
logged.

**The verdict is the panel's.** The agent does not decide whether a site "works":
it reports facts, while thresholds, retries and incidents stay in the panel,
under the same rules as any other monitor.

The honest flip side: the probe task carries this monitor's custom headers and
basic-auth password, if set. That is the same data the site on that node already
uses, but it is worth knowing.

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
