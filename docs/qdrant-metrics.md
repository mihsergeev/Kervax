# Qdrant metadata

The Services page shows Qdrant version, collection count, approximate point counts,
indexed vector counts, collection status and optimizer status when the agent can
reach the instance's ordinary read-only metadata API. Open the server's
**Databases and stores** section to see the collection table.

Point counts are approximate. A point can have multiple vectors; the two counters
are displayed separately. Metadata comes from the responding instance and is not
added across nodes or replicas. An absent counter is unknown, not zero.

The agent probes only Qdrant workloads it already discovered:

- Running Docker containers, including host networking.
- Running Kubernetes pods whose cgroup is on the local node.
- A native Qdrant process proven to share the agent's network namespace.

Only literal private or loopback addresses on HTTP port 6333 are accepted. The
collector does not follow redirects, use an HTTP proxy, resolve hostnames or read
credentials. Custom ports and TLS endpoints are not collected yet. An API that
requires authentication remains protected and is shown as requiring access.
An unavailable or partially collected API is shown explicitly; this does not
create a new database failure alert or replace server availability monitoring.

Only GET `/`, `/collections` and `/collections/{name}` are used. No point payloads,
vectors, searches, exact counts or configuration changes are requested. The scan
is limited to four instances, eight collections per instance, twenty requests,
256 KiB per response and 2 MiB in total. A request has a 600 ms timeout; the scan
has a shared 2.8 second deadline. A limited result is marked as partial.

Reference: [Qdrant collection metadata API](https://api.qdrant.tech/v-1-17-x/api-reference/collections/get-collection).
