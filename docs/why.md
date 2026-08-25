# Why another monitoring panel

[Русская версия](why.ru.md) · [README](../README.md) · [Security](security.en.md)

Zabbix, Prometheus with Grafana and Uptime Kuma solve their tasks and solve them
well. The problem is different: a small fleet typically ends up with **two**
systems — one checking sites from outside, another collecting server metrics. The
data sits apart, and correlating a slow site with load on a particular machine is
left to a human.

## What the panel takes over

| Task | With Kervax | The usual way |
| --- | --- | --- |
| A site is down | one monitor: status, keyword, response time | a blackbox exporter plus a rule plus a dashboard |
| Is it down for everyone | probes from your own locations, with the affected region named | someone asks in chat and opens it on their phone |
| The certificate expires | escalating reminders, 14 / 7 / 1 days | a calendar entry, or nothing |
| The domain expires | the same, grouped by the name you renew | the registrar's e-mail, filtered as spam |
| Server metrics | one agent, no port opened, no SSH from the panel | an exporter per node plus a scrape config |
| What runs on the node | containers, pods, web servers, databases, queues | `ssh` and `docker ps` |
| Are the backups alive | restic status per node, and whether it fit the window | you find out on the day you need them |
| Restore credentials | a vault encrypted in the browser | a text file somewhere, or in someone's head |
| A node stopped reporting | "Action needed" names the node and the reason | the graph stays flat and the gap goes unnoticed |
| Alerts | debounced, snoozed, muted per type, routed by role | a channel that stops being read |
| Adding a machine | one command from the panel | an Ansible role that has to be written first |

## External checks and host metrics

An external check records that the site answered in 2.8 s, but knows nothing
about the 40 % iowait on the database node at that moment. The agent sees the
iowait but not its effect on response time. Kervax stores both in one database on
a shared timeline: from a monitor in a degraded state to the metrics of the
machine behind it is two clicks.

For the same reason the panel tracks things that are not metrics: TLS
certificate validity, domain registration, the time of the last backup. These
fail rarely but completely, and rarely have a dashboard of their own.

## Alerts

An alert channel is only useful while it is still read, so noise is limited in
the firing logic itself:

- a threshold has to be **held** for a configured time: a single CPU spike
  within one collection interval is not an incident;
- a failed check is retried, and the alert waits for N failures in a row;
- expiry warnings are grouped by the registrable domain, so five monitors on
  `*.example.com` produce one message, not five;
- every alert type can be muted, snoozed, or scoped to a group;
- routing follows the same roles and groups the panel enforces in its API, so an
  account receives alerts only for its own objects.

## Checking its own state

Monitoring degrades quietly: an agent stops reporting a new metric because it is
several versions behind the panel, a helper script on a node predates the feature
that needs it, a probe location stops resolving DNS. Nothing turns red — some of
the data simply stops arriving.

These cases are handled separately. Outdated agents and helpers, unreachable
probe locations, clock drift and backups outside their window all land in
**Action needed** on the home page, together with the command that resolves
them.

## What the panel does not do

The limits are worth knowing before an install rather than after.

- **It is not an APM.** No traces, no per-endpoint latency inside the
  application, no profiling. It will not tell you which SQL query became slow.
- **It is not a log system.** Specific items are read — OOM events, unit states,
  container logs on request — but logs are neither shipped nor indexed.
- **It is not a replacement for Prometheus** when application metrics already
  live there. Kervax covers the infrastructure layer — host, site, certificate,
  backup — and runs alongside without conflict.
- **It is not a multi-tenant SaaS.** One installation covers one team's
  infrastructure: roles and groups scope visibility, but they are not billing
  boundaries.
- **It is not long-term storage.** Raw samples are pruned by retention (30 days
  by default, configurable). Uptime, incidents and expiry history are kept;
  per-minute metrics from past years are not.

## Requirements

Docker with Compose and a reverse proxy terminating TLS; a domain is optional.
No cloud account or external service is required, and the agent listens on no
port. History is stored in PostgreSQL next to the panel.

Everything else is in the [README](../README.md).
