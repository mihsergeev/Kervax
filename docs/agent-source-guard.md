# Agent source guard (optional)

The guard checks agent source addresses separately from agent tokens. It is disabled
by default. Existing updates and quickstart runs keep that setting unchanged.
Use it behind the supported frontend and Caddy topology with exact Compose project
and service labels. It does not trust whole Docker/private subnets.

For an existing running installation:

1. Copy `ops/agent-proxy-peers.env.example` to
   `/etc/kervax/agent-proxy-peers.env`, owned by root and mode `0600`.
   Set the exact Compose project/service labels, edge network name and host data
   directory. Do not select a container merely by its name or image.
2. Run `sudo sh ops/agent-proxy-peers-setup.sh --prepare "$PWD"`.
   Verify `data/agent_proxy_peers.json` is root-owned, mode `0644`, and contains
   only the frontend addresses on the backend network and Caddy addresses on the
   frontend edge network. Both IPv4 and IPv6 are supported.
3. Set `KERVAX_AGENT_SOURCE_GUARD=1` in the panel `.env`, then use the usual
   deployment/update command. Configure every agent's permitted address first,
   including separately verified relay or NAT egress addresses where needed.
   Keep at least one agent reporting before enabling this mode. `ops/deploy.sh`
   captures agents seen within 90 seconds and requires each of them to report again
   after container recreation (up to 60 seconds), with the guard enabled in the
   running backend. Failure stops the fleet rollout and attempts the existing
   rollback only when its migration safety checks allow it. Plan database-changing
   upgrades separately; health alone cannot confirm this change.

The host timer refreshes the snapshot every 30 seconds. The update scripts refresh
it before container recreation and again afterwards. Missing, ambiguous, or unhealthy
topology replaces the peer lists with an invalid failure marker and fails the refresh.
It does not publish an empty valid topology as a fallback. Once enabled, missing or stale
peer data denies agent requests; it never disables the guard automatically.

For a manual container recreation, refresh the snapshot immediately afterwards:
`sudo systemctl restart kervax-agent-proxy-peers.service`.
Use `systemctl status kervax-agent-proxy-peers.timer` and the service journal to
check refresh failures. No firewall changes or server reboot are required.
