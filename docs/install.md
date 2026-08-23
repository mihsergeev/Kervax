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
> written in the name itself: `95.216.199.167.sslip.io` resolves to
> `95.216.199.167`. Nothing to register, nothing to configure, and Let's Encrypt
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
sudo docker network create caddy
sudo mkdir -p /srv/caddy && cd /srv/caddy
```

Put a `compose.yml` there:

```yaml
services:
  caddy:
    image: lucaslorentz/caddy-docker-proxy:2.10-alpine
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
KERVAX_DOMAIN=95.216.199.167.sslip.io
KERVAX_PANEL_URL=https://95.216.199.167.sslip.io
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
sudo docker compose -f compose.yml -f compose.scale.yml -f compose.ghcr.yml -f compose.ghcr-scale.yml up -d
```

To stop repeating the `-f` flags, put this in `.env`:

```ini
COMPOSE_FILE=compose.yml:compose.ghcr.yml:compose.caddy.yml
```

### 5. Check

```bash
curl https://95.216.199.167.sslip.io/api/health
```

`{"status":"ok","version":"1.1.0"}` means the panel, the database and the
certificate are all in place. The first certificate takes up to a minute — if
nothing answers immediately, wait and retry.

---

## First sign-in

Open the panel's address and sign in as `admin` with the password from `.env`.
Right after that:

1. **Change the password** (⚙ → "Change password") — it ends every other session.
2. **Turn on 2FA** in the same menu. The panel governs access to your
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

## Your first site

**"Add monitor"** — an address is enough: `example.com` or
`https://example.com/health`. The scheme is detected automatically, the TLS
certificate and the domain registration start being tracked on their own, and
history and uptime begin with the very first check.

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

```bash
cd /opt/kervax && sudo git pull
sudo docker compose up -d --build   # or without --build on prebuilt images
```

Database migrations run themselves at startup. Your data lives in `./data` and is
untouched by upgrades.

**Backups.** ⚙ → "Backup" exports the configuration (monitors, servers, settings)
and schedules automatic copies. Back up metric history together with the `data/`
directory.

**Removing it.**

```bash
cd /opt/kervax && sudo docker compose down -v && cd / && sudo rm -rf /opt/kervax
```

---

## When something goes wrong

| Symptom | Cause | What to do |
| --- | --- | --- |
| `https://…` does not answer, ACME errors in Caddy's log | port 80 or 443 is taken, or the domain does not resolve to this server | `sudo ss -lntp \| grep -E ':80\|:443'`, `getent hosts <domain>` |
| No certificate for your own domain | the A record has not propagated | wait for DNS, check `dig +short <domain>` |
| `/api/health` returns 200 but the page will not open | your address is not in `KERVAX_ALLOW_IPS` | add your IP, or clear the list |
| The `backend` container keeps restarting | a weak or empty secret in `.env` | `docker compose logs backend \| tail -20` says which one |
| The build dies without a clear error | out of memory | use the prebuilt images (`compose.ghcr.yml`) |
| The agent installed but the node is missing | the node cannot reach the panel | on the node: `systemctl status kervax-agent`, `journalctl -u kervax-agent -n 30` |
| The install command reads `https://ПАНЕЛЬ` | `KERVAX_PANEL_URL` is not set | set it in `.env`, then `docker compose up -d backend` |

Full logs:

```bash
cd /opt/kervax && sudo docker compose logs --tail=100 backend scheduler frontend
```
