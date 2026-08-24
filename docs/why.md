# Why another monitoring panel

[Русская версия](why.ru.md) · [README](../README.md) · [Security](security.en.md)

Zabbix, Prometheus with Grafana and Uptime Kuma all exist and all work. Kervax
was not written because they are bad; it was written because a small fleet
usually ends up running **two** of them — one that pings the sites and one that
graphs the servers — and neither can answer "the shop is slow, is it us?".

## What the panel takes over

| Task | With Kervax | The usual way |
| --- | --- | --- |
| A site is down | one monitor: status, keyword, response time | a blackbox exporter plus a rule plus a dashboard |
| Is it down for everyone | probes from your locations, "partial" with the region named | someone asks in chat and opens it on their phone |
| The certificate expires | escalating reminders, 14 / 7 / 1 days | a calendar entry, or nothing |
| The domain expires | the same, grouped by the name you renew | the registrar's e-mail, filtered as spam |
| Server metrics | one agent, no port opened, no SSH from the panel | an exporter per node plus a scrape config |
| What runs on the node | containers, pods, web servers, databases, queues | `ssh` and `docker ps` |
| Are the backups alive | restic status per node, and whether it fit the window | you find out on the day you need them |
| Restore credentials | a vault encrypted in the browser | a text file somewhere, or in someone's head |
| A node fell out of the fleet | "Action needed" says which and why | the graph stays flat and nobody notices |
| Alerts | debounced, snoozed, muted per signal, routed per person | a channel everyone learned to ignore |
| Adding a machine | one command from the panel | a role in Ansible you write first |

## Two halves of the same question

An outside check knows the site answered in 2.8 s but not that the database node
had 40 % iowait at that moment. An agent on the node knows the iowait but not
that anyone noticed. Kervax keeps both in one place: the monitor that went
degraded and the machine behind it are two clicks apart, on the same timeline.

That is also why the panel watches things that are not metrics at all —
certificates, domain registrations, backup freshness. They break rarely, they
break completely, and they are exactly what nobody has a dashboard for.

## Alerts you don't learn to ignore

An alert channel is only useful while people still read it. So:

- a threshold has to be **held**, not touched — a CPU spike for one tick is not
  an incident;
- a failed check is retried, and the alert waits for N failures in a row;
- expiry warnings are grouped by the registrable domain, so five monitors on
  `*.example.com` produce one message, not five;
- every alert type can be muted, snoozed for an hour, or scoped to a group;
- people can get only their own alerts — routing follows the same roles and
  groups that the panel enforces in its API.

## The panel tells you when it has fallen behind

Monitoring degrades silently: an agent stops reporting a new metric because it
is three versions old, a helper script on a node predates the feature that needs
it, a probe location quietly stopped resolving DNS. Nothing is red — there is
simply less truth on the screen than you think.

Kervax treats that as a first-class problem. Outdated agents and helpers, broken
probe locations, clocks that drifted, backups that ran past their window all land
in **Action needed** on the home page, with the command that fixes them.

## What it does not do

Being honest about this is cheaper than disappointing you later.

- **Not an APM.** No traces, no per-endpoint latency inside your application, no
  profiling. If you need to know which SQL query got slow, this is not the tool.
- **Not a log system.** It reads a few specific things (OOM kills, unit states,
  container logs on request) but it does not ship or index logs.
- **Not a replacement for Prometheus** when you already have application metrics
  in it. Kervax watches infrastructure — the host, the site, the backup — not
  your business counters. The two coexist fine.
- **Not a multi-tenant SaaS.** One installation is one team's infrastructure.
  Roles and groups scope what a person sees; they are not billing boundaries.
- **Not a long-term data warehouse.** Samples are pruned (30 days by default,
  configurable). Uptime, incidents and expiry history survive; raw per-minute
  metrics do not, on purpose.

## What you need

Docker with Compose, a domain, and a reverse proxy that terminates TLS. No cloud
account, no external service, no agent listening on a port. Postgres holds the
history and lives next to the panel.

Everything else is in the [README](../README.md).
