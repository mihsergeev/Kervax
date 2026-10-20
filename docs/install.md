# Installing Kervax

[Русская версия](install.ru.md) · [README](../README.md) · [Why](why.md)

Ten minutes on a fresh server gets you a panel on https with a real certificate —
no domain of your own required, and not one line of nginx config.

---

## What you need

- A Linux server with Docker support. **2 GB of RAM is enough** — verified on
  Ubuntu 26.04 with 2 GB: the panel builds from source and runs.
- **Ports 80 and 443 free.** Caddy takes them and obtains a Let's Encrypt
  certificate on its own. Already running nginx? See
  [«I already have a proxy»](#i-already-have-a-proxy).
- root (or sudo).
- A domain is **optional**: `<server-ip>.sslip.io` is used by default.

> **What sslip.io is.** A public DNS service that answers with the address
> written in the name itself: `203.0.113.20.sslip.io` resolves to
> `203.0.113.20`. Nothing to register, nothing to configure, and Let's Encrypt
> issues an ordinary certificate for such a name. For a permanent installation
> use your own domain — but to see the panel, this is enough.

---

## The short way: one command

```bash
curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh
```

It prints the panel's address and the admin password. Nothing else to type.

With your own domain (point an A record at the server first):

```bash
curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh -s -- --domain kervax.example.com
```

With the address allow-list from the start — recommended if the panel faces the
internet:

```bash
curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh -s -- --allow-ips "203.0.113.10 198.51.100.23"
```

### What the script does

No magic — every step can be done by hand (see the next section):

1. installs Docker if it is missing;
2. checks that ports 80 and 443 are free — otherwise the certificate will not be
   issued, and it is better to learn that now;
3. starts `caddy-docker-proxy`, which terminates TLS and routes by container
   labels;
4. clones the repository into `/opt/kervax`;
5. writes an `.env` with a random admin password, JWT secret and database
   password;
6. starts the panel from prebuilt images (`--build` builds from source instead);
7. waits for `https://<domain>/api/health` and prints the credentials.


### In a local network: `--local-ca`

A lab VM or a server in a home or office LAN is out of Let's Encrypt's reach: it
cannot connect back to a private address, so no certificate is issued. The default
name does not help either. `192.168.1.10.sslip.io` resolves to a private address,
and a home router with DNS rebinding protection (OpenWrt and anything else on
dnsmasq, many ISP routers) drops exactly such answers: the name does not resolve
at all. The panel comes up, https does not, and agents cannot be installed.

```bash
curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh -s -- --local-ca
```

What changes:

- the panel is addressed by the server's LAN IP: `https://192.168.1.10`. No DNS
  name means nothing to resolve, nothing for the router to block and no
  `/etc/hosts` to edit;
- the certificate comes from caddy's own internal CA. It is set for the panel's
  site only (the `compose.localca.yml` overlay puts `tls internal` on the panel's
  container), so other sites on a shared caddy keep Let's Encrypt;
- the CA root is copied to `/opt/kervax/data/panel-ca.crt`, and the script prints
  its SHA-256 fingerprint;
- **agents need no manual step.** The install command in the panel carries the
  root and passes it with `--ca-file`; `install.sh` adds it to the node's system
  trust store, and the agent, the helpers and `curl` verify the panel with it.
  Certificate checks are never switched off. `install.sh --uninstall` removes the
  root again;
- a repeated run (an upgrade) keeps the mode, `--local-ca` is not needed again.

Your browser does not know this CA yet. Either accept the warning once, or import
the root as a trusted authority: it is served at `https://<panel>/api/agent/ca.crt`
(compare the fingerprint with the one quickstart printed). Any other Linux machine
that should talk to the panel:

```bash
sudo cp panel-ca.crt /usr/local/share/ca-certificates/kervax-panel.crt && sudo update-ca-certificates
```

On RHEL and Fedora the place is `/etc/pki/ca-trust/source/anchors/` and the command
`update-ca-trust`.

**A name instead of an IP.** `--local-ca --domain kervax.lan` works as well, but
then the name must resolve to the panel on every agent machine and on your
computer: a record in your local DNS, or a line in `/etc/hosts` on each of them:

```
192.168.1.10  kervax.lan
```

A `<private-IP>.sslip.io` name only works where the router lets such answers
through. With OpenWrt that is the "Rebind protection" switch or a
`rebind_domain` exception for `sslip.io`; usually the IP is simpler.

**Your own CA.** If the panel's certificate is signed by a corporate CA, put its
root (PEM) into `data/panel-ca.crt`: the install command in the panel picks it up
the same way.

---

## By hand

The same thing step by step, if you want to see every one of them.

### 1. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
```

### 2. Caddy for TLS

The panel does not deal with certificates — a reverse proxy sits in front of it.
`caddy-docker-proxy` reads container labels and obtains certificates itself.

```bash
# --ipv6 matters: in an IPv4-only network docker forwards IPv6 connections through
# its userland proxy and caddy sees the bridge gateway instead of the client, so an
# IP allow-list lets every IPv6 client in. If your docker cannot do --ipv6, publish
# caddy's ports as "0.0.0.0:80:80" and "0.0.0.0:443:443" instead.
sudo docker network create --ipv6 caddy
sudo mkdir -p /srv/caddy && cd /srv/caddy
```

Put a `compose.yml` there:

```yaml
services:
  caddy:
    image: lucaslorentz/caddy-docker-proxy:2.13.1-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    environment:
      CADDY_INGRESS_NETWORKS: caddy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./data:/data
    networks: [caddy]
networks:
  caddy:
    external: true
```

```bash
sudo docker compose up -d
```

### 3. Repository and settings

```bash
sudo git clone https://github.com/mihsergeev/Kervax.git /opt/kervax
cd /opt/kervax && sudo cp .env.example .env
```

Fill in five lines of `.env`:

```ini
KERVAX_ADMIN_PASSWORD=<at least 12 characters>
KERVAX_JWT_SECRET=<openssl rand -hex 32>
KERVAX_DB_PASSWORD=<any long random string>
KERVAX_DOMAIN=203.0.113.20.sslip.io
KERVAX_PANEL_URL=https://203.0.113.20.sslip.io
```

The panel checks the first three at startup and **refuses to run** on empty or
default values. `KERVAX_PANEL_URL` is the same domain with a scheme: the agent
install command and the deep links in alerts are built from it.

The sixth line is who may open the panel:

```ini
KERVAX_ALLOW_IPS=203.0.113.10 198.51.100.23
```

**Leaving it empty means "everyone"** — the only thing guarding the panel is then
the password and the second factor. Agent endpoints (`/api/agent/*`) are never
part of that list: agents come from the addresses of monitored nodes and
authenticate with their own tokens.

Take the address the **server** sees you as, not the one an IP-lookup service
reports: on a machine with more than one uplink they differ, and the panel will
lock you out along with everyone else. The simplest source is the ssh session you
already have:

```bash
ssh <server> 'echo $SSH_CLIENT' | awk '{print $1}'
```

### 4. Start

From prebuilt images — fast, nothing is compiled:

```bash
sudo docker compose -f compose.yml -f compose.ghcr.yml -f compose.caddy.yml up -d
```

Or from source — slower (a few minutes: the Go agent for two architectures and
the frontend), but you know exactly what you are running:

```bash
sudo docker compose -f compose.yml -f compose.caddy.yml up -d --build
```

In scale mode (several workers and a separate scheduler) there are more files:

```bash
sudo docker compose -f compose.yml -f compose.scale.yml -f compose.ghcr.yml -f compose.ghcr-scale.yml -f compose.caddy.yml up -d
```

`compose.caddy.yml` is as mandatory here as the rest: it carries the labels Caddy
finds the panel by. Always list the **whole** set — a file left out does not mean
"keep what was there", it recreates the containers without whatever that file
described.

Now a **required step**: record your set of files in `.env` so you never have to
repeat those flags.

```ini
COMPOSE_FILE=compose.yml:compose.ghcr.yml:compose.caddy.yml
```

Without this line, `docker compose up -d` in the panel directory sees `compose.yml`
alone: it builds images from source instead of pulling the released ones and
recreates the containers without the Caddy network and its labels — the panel
silently disappears from its domain. That is exactly what an upgrade looks like,
and one line prevents it. The quick path (`quickstart.sh`) writes it for you; the
line must list the same files you started the panel with.

### 5. Check

From your own machine:

```bash
curl https://203.0.113.20.sslip.io/api/health
```

`{"status":"ok","version":"1.0.0","db":"ok"}` means the panel, the database and
the certificate are all in place: `db` comes from a real query, so an unreachable
database makes the panel answer `503` with `"status":"degraded"` instead of `ok`.
The first certificate takes up to a minute — if nothing answers immediately, wait
and retry.

If you filled in `KERVAX_ALLOW_IPS`, this check **will not work from the server
itself** — its own address is not on the list, so Caddy drops the connection
(`curl` reports code `000`). That is intended. On the server, ask the panel from
the inside:

```bash
cd /opt/kervax && sudo docker compose exec -T backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/health').read().decode())"
```

---

## First sign-in

Open the panel's address and sign in as `admin` with the password from `.env`.
Right after that:

1. **Change the password** (⚙ → "My account" → "Password") — it ends every other session.
2. **Turn on 2FA** in the same window ("App code"). The panel governs access to your
   infrastructure; one password is not enough for it.
3. If you left `KERVAX_ALLOW_IPS` empty, put your address in and apply:
   `sudo docker compose up -d frontend`.

## Your first monitored server

Press **"Add server"**, give it a name, and the panel shows a single command. Run
it as root on the machine you want to watch:

```bash
curl -fsSL https://<your-panel>/api/agent/install.sh | sh -s -- https://<your-panel> <token>
```

What happens: a static agent binary is downloaded, a systemd unit is created
under the unprivileged `kervax` user, and the helper scripts are installed
(backups, database inventory, web-server domains, clock sync). Within seconds the
machine shows up in the panel with all its metrics — no exporters to configure,
no scrape targets to declare.

The agent only connects **outwards**, over HTTPS, to the panel. It opens no port,
runs no shell, and the panel stores nothing but a SHA-256 of its token.

A panel installed with `--local-ca` shows a longer command: it starts with the CA
root (`echo ... | base64 -d > /tmp/kervax-panel-ca.crt`) and passes it to
`install.sh` with `--ca-file`. Run it as is, see
[In a local network](#in-a-local-network---local-ca).

If the server cannot reach the panel directly (for example, its address is blocked
somewhere on the way to the panel), choose how the agent goes there in the same window:

* **Through a relay** - another server of yours that reaches the panel. Switch on
  **"Relay for agents"** in that server's settings: the `relay-setup` helper shows up in
  "Action needed" and the `kervax_helpers.yml` playbook installs it. It is a small HTTP
  CONNECT proxy (python3, nothing to install) that lets through only to the panel, only
  from the servers that chose it here, and only with the login the panel issued. When a
  firewall (ufw or firewalld) is active, the helper opens the port for exactly those
  addresses. Choosing the relay in this window adds the server to its list right away.
  Switching the relay off removes the proxy, its firewall rules and the helper.
* **Through a location** - the proxy of one of the locations (the Locations window).
* **Own proxy** - `http://user:password@host:port` (`https://` and `socks5://` work too).
  The panel never sees it: the browser puts it into the command.

The login and password go into the command by themselves: it downloads through the proxy
and passes `--proxy` to `install.sh`, which writes `proxy=` into the agent config. Reports,
commands and updates all go through the proxy (agent 2.44+). For the agent itself,
`HTTPS_PROXY` in its environment works as well.

Behind a proxy the panel sees the proxy address, not the server's. Enter the server's
external IP in the same window: the country and the outside checks use it, and a relay
lets in only known addresses. The proxy address has to be allowed in the panel firewall,
if it has one. A server whose agent has not reported for 10 minutes after it was added
gets a hint in its card with a button that opens this window.

## Your first site

**"Add monitor"**: type an address (`example.com`,
`https://example.com/health`, or `db.example.com:5432` for a port) and press
"Check". Before anything is saved, the panel opens the address from the panel
itself, from inside the server where the domain was found, and from the
locations, shows how each of them went and picks the one that works. The scheme
is detected automatically, the TLS certificate and the domain registration are
tracked on their own, and history and uptime begin with the very first check.

---

## Next

**Your own domain instead of sslip.io.** Point an A record at the server, update
`KERVAX_DOMAIN` and `KERVAX_PANEL_URL` in `.env`, then
`sudo docker compose up -d frontend backend`. Caddy fetches the new certificate
itself.

**I already have a proxy.** Skip the `compose.caddy.yml` overlay — the panel is
then published on the host (`127.0.0.1:8080` by default, `KERVAX_BIND` changes
it) and you proxy it with your own nginx/traefik. Inside the container the
frontend listens on **8080** — it runs without root.

**Alerts.** ⚙ → "Alerts": a Telegram bot or a webhook. The text of each alert
type, the thresholds and the scope (all nodes / a group / a selection) are
editable in the panel.

**Upgrading.**

On prebuilt images:

```bash
cd /opt/kervax && sudo git pull && sudo docker compose pull && sudo docker compose up -d
```

From source:

```bash
cd /opt/kervax && sudo git pull && sudo docker compose up -d --build
```

The `docker compose pull` is not optional: `up -d` on its own starts the image
already on disk, so only the repository files change — the panel stays on the
old version without saying a word.

Both commands rely on the `COMPOSE_FILE` line in `.env` (see "Start"). If it is
missing, add it — otherwise the upgrade rebuilds the panel from source and brings
it up without Caddy, that is, without its domain. To confirm the file set is
visible:

```bash
cd /opt/kervax && sudo docker compose config --services
```

It must list `db`, `backend`, `frontend` (plus `scheduler` in scale mode), and
after the upgrade the version must be the new one:

```bash
sudo docker compose exec -T backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/health').read().decode())"
```

Database migrations run themselves at startup. Your data lives in `./data` and is
untouched by upgrades.

**Backups.** ⚙ → "Backup" exports the configuration (monitors, servers, locations,
settings) and schedules automatic copies. Back up metric history together with the
`data/` directory.

Accounts are **not** part of a backup: there is no reason to keep passwords and
second-factor secrets in a downloadable file. Keep that in mind when restoring onto a
new server — monitors and settings come back, but users, their roles and permissions
have to be created again.

**Removing it.**

```bash
cd /opt/kervax && sudo docker compose down -v && cd / && sudo rm -rf /opt/kervax
```

**Removing the agent from a monitored node.** Deleting a server in the panel
revokes its token, but the agent itself stays on the machine and keeps knocking.
To take it away with everything that was installed (service, helper units, binary,
configs, sudoers, the `kervax` user, the docker proxy):

```bash
curl -fsSL https://<your-panel>/api/agent/install.sh | sudo sh -s -- --uninstall
```

If the node ran several agents (a second one reporting to another panel), remove
a single instance by name: `--uninstall panel2` — the shared binary and the other
instances stay untouched.

If you plan to install the panel again on the **same domain**, leave
`/srv/caddy/data` in place: it holds the issued certificates. Let's Encrypt
allows five issuances for the same name per 168 hours, and every reinstall that
deletes this directory spends one. Once the limit is gone, the domain simply
stops opening — with `rateLimited` in the Caddy log and the time the limit
lifts.

---

## When something goes wrong

| Symptom | Cause | What to do |
| --- | --- | --- |
| `https://…` does not answer, ACME errors in Caddy's log | port 80 or 443 is taken, or the domain does not resolve to this server | `sudo ss -lntp \| grep -E ':80\|:443'`, `getent hosts <domain>` |
| No certificate for your own domain | the A record has not propagated | wait for DNS, check `dig +short <domain>` |
| The domain will not open, Caddy logs say `rateLimited` | Let's Encrypt limit spent: 5 issuances per name per 168 hours (usually after several reinstalls) | use another name or wait for the time in the message; stop deleting `/srv/caddy/data` |
| Connection drops: empty reply, `curl` reports code `000` | your address is not in `KERVAX_ALLOW_IPS` — Caddy drops the connection silently, without revealing that a panel is here | add your IP, or clear the list |
| The `backend` container keeps restarting | a weak or empty secret in `.env` | `docker compose logs backend \| tail -20` says which one |
| The build dies without a clear error | out of memory | use the prebuilt images (`compose.ghcr.yml`) |
| The agent installed but the node is missing | the node cannot reach the panel | on the node: `systemctl status kervax-agent`, `journalctl -u kervax-agent -n 30` |
| The install command reads `https://ПАНЕЛЬ` | `KERVAX_PANEL_URL` is not set | set it in `.env`, then `docker compose up -d backend` |
| Panel in a LAN: Caddy logs `no valid A records found for 192.168.1.10.sslip.io` or `Timeout during connect` | Let's Encrypt cannot reach a private address; a router with DNS rebinding protection does not resolve `<private-IP>.sslip.io` | reinstall with `--local-ca`, see [In a local network](#in-a-local-network---local-ca) |
| `curl: (60) SSL certificate problem` on a node, or the agent log says `x509: certificate signed by unknown authority` | the panel's certificate comes from its internal CA, and the node does not trust it | run the install command from the panel again (it carries the root), or add `data/panel-ca.crt` to the node's trust store |
| `https://192.168.1.10` fails with `tlsv1 alert internal error` | a panel opened by IP sends no SNI, and Caddy has no certificate for it | the `compose.localca.yml` overlay must be in `COMPOSE_FILE` (it sets `default_sni`) |

Full logs:

```bash
cd /opt/kervax && sudo docker compose logs --tail=100 backend scheduler frontend
```
