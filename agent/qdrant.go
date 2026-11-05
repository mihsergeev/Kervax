package main

// Qdrant's ordinary read-only metadata API, without credentials or privilege changes.
// Only discovered local workloads are probed, at the standard HTTP port. No point,
// payload, vector, search, count, telemetry or configuration endpoint is requested.

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	qdrantMaxInstances   = 4
	qdrantMaxCollections = 8
	qdrantMaxRequests    = 20
	qdrantMaxBody        = 256 << 10
	qdrantMaxBytes       = 2 << 20
	qdrantScanTimeout    = 2800 * time.Millisecond // leave time for bounded parsing/cleanup within a three-second scan
	qdrantRequestTimeout = 600 * time.Millisecond
)

type qdrantCollection struct {
	Name                string `json:"name"`
	PointsCount         *int64 `json:"points_count,omitempty"`
	IndexedVectorsCount *int64 `json:"indexed_vectors_count,omitempty"`
	Status              string `json:"status,omitempty"`
	OptimizerStatus     string `json:"optimizer_status,omitempty"`
}

type qdrantInfo struct {
	Scope             string             `json:"scope"`            // node: API reached on one discovered local workload, never a cluster rollup
	State             string             `json:"state"`            // ok | partial | auth_required | unavailable; not a database health verdict
	Reason            string             `json:"reason,omitempty"` // api | timeout | limit | address; no response body or error text
	Version           string             `json:"version,omitempty"`
	CollectionsTotal  *int               `json:"collections_total,omitempty"`
	Collections       []qdrantCollection `json:"collections,omitempty"`
	PointsApproximate bool               `json:"points_approximate"`
	Truncated         bool               `json:"truncated,omitempty"`
}

var qdrantVersion = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?$`)

type qdrantTarget struct{ source, ip string }

func qdrantTargets(dk *dockerInfo, kube *kubeInfo, procs map[int]procSample, proc string) []qdrantTarget {
	var out []qdrantTarget
	seen := map[string]bool{}
	add := func(source, ip string) {
		// Empty addresses stay explicit unknowns, rather than falling back to an unrelated localhost.
		key := ip
		if key == "" {
			key = source
		}
		if !seen[key] && len(out) < qdrantMaxInstances {
			seen[key] = true
			out = append(out, qdrantTarget{source, ip})
		}
	}
	if dk != nil && dk.Access {
		for _, c := range dk.Containers {
			if c.State == "running" && vectorDBImage(c.Image) == "qdrant" {
				ip := c.ip
				if c.hostNetwork {
					ip = "127.0.0.1"
				}
				add("контейнер "+c.Name, ip)
			}
		}
	}
	if kube != nil && kube.Access {
		for _, p := range kube.Pods {
			if p.local && p.Phase == "Running" && vectorDBImage(p.Image) == "qdrant" {
				add("под "+p.NS+"/"+p.Name, p.ip)
			}
		}
	}
	// A native Qdrant must demonstrably share the agent's network namespace.
	// /proc comm alone also finds container processes and is not enough for localhost.
	self := fileIno(filepath.Join(proc, "self/net/dev"))
	if self != "" {
		for pid, p := range procs {
			if p.comm != "qdrant" {
				continue
			}
			pd := filepath.Join(proc, strconv.Itoa(pid))
			kind, _ := procOwner(sysStr(filepath.Join(pd, "cgroup")))
			if kind == "container" || kind == "pod" {
				continue
			}
			if fileIno(filepath.Join(pd, "net/dev")) == self {
				add("хост", "127.0.0.1")
			}
		}
	}
	return out
}

func collectQdrantServices(dk *dockerInfo, kube *kubeInfo, procs map[int]procSample, proc string) []serviceInfo {
	targets := qdrantTargets(dk, kube, procs, proc)
	if len(targets) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), qdrantScanTimeout)
	defer cancel()
	cl := newQdrantClient()
	defer cl.CloseIdleConnections()
	return probeQdrantTargets(ctx, targets, cl)
}

func probeQdrantTargets(ctx context.Context, targets []qdrantTarget, cl *http.Client) []serviceInfo {
	b := &qdrantBudget{requests: qdrantMaxRequests, bytes: qdrantMaxBytes}
	var out []serviceInfo
	for _, t := range targets {
		info := &qdrantInfo{Scope: "node", State: "unavailable", PointsApproximate: true}
		if endpoint := qdrantEndpoint(t.ip); endpoint != "" {
			info = readQdrant(ctx, cl, endpoint, b)
		} else {
			info.Reason = "address"
		}
		out = append(out, serviceInfo{Kind: "qdrant", Source: t.source, Qdrant: info})
	}
	return out
}

func newQdrantClient() *http.Client {
	// Do not use environment HTTP proxies or follow redirects. The endpoint builder
	// accepts only literal private/loopback IPs, so there is no DNS or URL input.
	return &http.Client{Timeout: qdrantRequestTimeout,
		Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: qdrantRequestTimeout}).DialContext,
			MaxResponseHeaderBytes: 16 << 10, DisableKeepAlives: true},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

func qdrantEndpoint(raw string) string {
	ip, err := netip.ParseAddr(raw)
	if err != nil || ip.Zone() != "" || ip.Is4In6() || (!ip.IsPrivate() && !ip.IsLoopback()) {
		return ""
	}
	return "http://" + net.JoinHostPort(ip.String(), "6333")
}

type qdrantBudget struct{ requests, bytes int }

// Returns a closed error vocabulary. Raw HTTP responses, credential errors and URLs never leave.
func (b *qdrantBudget) get(ctx context.Context, cl *http.Client, endpoint string, out any) string {
	if b.requests <= 0 || b.bytes <= 0 {
		return "limit"
	}
	if ctx.Err() != nil {
		return "timeout"
	}
	b.requests--
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "api"
	}
	resp, err := cl.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return "timeout"
		}
		if e, ok := err.(net.Error); ok && e.Timeout() {
			return "timeout"
		}
		return "api"
	}
	defer resp.Body.Close()
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return "auth"
	}
	if resp.StatusCode != http.StatusOK {
		return "api"
	}
	limit := qdrantMaxBody
	if b.bytes < limit {
		limit = b.bytes
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, int64(limit+1)))
	b.bytes -= len(body)
	if len(body) > limit {
		return "limit"
	}
	if err != nil {
		if e, ok := err.(net.Error); ok && e.Timeout() {
			return "timeout"
		}
		return "api"
	}
	if json.Unmarshal(body, out) != nil {
		return "api"
	}
	return ""
}

func qdrantFailure(q *qdrantInfo, why string, available bool) {
	q.Reason = why
	if why == "auth" {
		q.State, q.Reason = "auth_required", ""
	} else if available {
		q.State = "partial"
	} else {
		q.State = "unavailable"
	}
}

func readQdrant(ctx context.Context, cl *http.Client, base string, budget *qdrantBudget) *qdrantInfo {
	q := &qdrantInfo{Scope: "node", State: "unavailable", PointsApproximate: true}
	var root struct {
		Title   string `json:"title"`
		Version string `json:"version"`
	}
	why := budget.get(ctx, cl, base+"/", &root)
	// Establish that this is Qdrant before sending collection metadata requests to a port.
	if why != "" {
		qdrantFailure(q, why, false)
		return q
	}
	if root.Title != "qdrant - vector search engine" || len(root.Version) > 64 || !qdrantVersion.MatchString(root.Version) {
		qdrantFailure(q, "api", false)
		return q
	}
	q.Version = root.Version
	var list struct {
		Status string `json:"status"`
		Result *struct {
			Collections []struct {
				Name string `json:"name"`
			} `json:"collections"`
		} `json:"result"`
	}
	why = budget.get(ctx, cl, base+"/collections", &list)
	if why != "" {
		qdrantFailure(q, why, true)
		return q
	}
	if list.Status != "ok" || list.Result == nil || list.Result.Collections == nil {
		qdrantFailure(q, "api", true)
		return q
	}
	n := len(list.Result.Collections)
	q.CollectionsTotal, q.State = &n, "ok"
	sort.Slice(list.Result.Collections, func(i, j int) bool { return list.Result.Collections[i].Name < list.Result.Collections[j].Name })
	for i, c := range list.Result.Collections {
		if i >= qdrantMaxCollections {
			q.Truncated = true
			break
		}
		// Never let a name turn a metadata request into another route, query or unbounded report.
		if c.Name == "" || len(c.Name) > 256 || strings.ContainsAny(c.Name, "/\\\x00\r\n") || c.Name == "." || c.Name == ".." {
			qdrantFailure(q, "api", true)
			continue
		}
		entry := qdrantCollection{Name: c.Name}
		var detail struct {
			Status string `json:"status"`
			Result *struct {
				PointsCount         *int64          `json:"points_count"`
				IndexedVectorsCount *int64          `json:"indexed_vectors_count"`
				Status              string          `json:"status"`
				OptimizerStatus     json.RawMessage `json:"optimizer_status"`
			} `json:"result"`
		}
		why = budget.get(ctx, cl, base+"/collections/"+url.PathEscape(c.Name), &detail)
		if why == "" && (detail.Status != "ok" || detail.Result == nil) {
			why = "api"
		}
		if why != "" {
			qdrantFailure(q, why, true)
			q.Collections = append(q.Collections, entry)
			if why == "auth" || why == "limit" || why == "timeout" {
				q.Truncated = i+1 < n
				break
			}
			continue
		}
		d := detail.Result
		if d.PointsCount != nil && *d.PointsCount >= 0 {
			entry.PointsCount = d.PointsCount
		}
		if d.IndexedVectorsCount != nil && *d.IndexedVectorsCount >= 0 {
			entry.IndexedVectorsCount = d.IndexedVectorsCount
		}
		switch d.Status {
		case "green", "yellow", "grey", "red":
			entry.Status = d.Status
		}
		// Optimizer errors can contain paths and arbitrary data. Export only the outcome.
		if string(d.OptimizerStatus) == `"ok"` {
			entry.OptimizerStatus = "ok"
		} else {
			var failure struct {
				Error *string `json:"error"`
			}
			if json.Unmarshal(d.OptimizerStatus, &failure) == nil && failure.Error != nil {
				entry.OptimizerStatus = "error"
			}
		}
		q.Collections = append(q.Collections, entry)
	}
	return q
}
