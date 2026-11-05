package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

const qdrantRoot = `{"title":"qdrant - vector search engine","version":"1.15.0"}`

func testQdrantRead(t *testing.T, handler http.HandlerFunc) *qdrantInfo {
	t.Helper()
	srv := httptest.NewServer(handler)
	defer srv.Close()
	cl := newQdrantClient()
	defer cl.CloseIdleConnections()
	return readQdrant(context.Background(), cl, srv.URL, &qdrantBudget{qdrantMaxRequests, qdrantMaxBytes})
}

func TestQdrantMetadataKeepsPointsAndVectorsSeparate(t *testing.T) {
	var paths []string
	q := testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.Header.Get("Authorization") != "" || r.Header.Get("Api-Key") != "" {
			t.Errorf("unexpected request method or credentials")
		}
		paths = append(paths, r.URL.EscapedPath())
		switch r.URL.Path {
		case "/":
			fmt.Fprint(w, qdrantRoot)
		case "/collections":
			fmt.Fprint(w, `{"status":"ok","result":{"collections":[{"name":"b"},{"name":"a"}]}}`)
		case "/collections/a":
			fmt.Fprint(w, `{"status":"ok","result":{"points_count":27,"indexed_vectors_count":54,"status":"green","optimizer_status":"ok","config":{"secret":"must-stay-local"}}}`)
		case "/collections/b":
			fmt.Fprint(w, `{"status":"ok","result":{"points_count":0,"status":"red","optimizer_status":{"error":"must-stay-local","time":"now"}}}`)
		default:
			t.Errorf("unexpected route %q", r.URL.Path)
		}
	})
	if q.State != "ok" || q.Scope != "node" || q.Version != "1.15.0" || !q.PointsApproximate || q.CollectionsTotal == nil || *q.CollectionsTotal != 2 || len(q.Collections) != 2 {
		t.Fatalf("wrong metadata: %+v", q)
	}
	a, b := q.Collections[0], q.Collections[1]
	if *a.PointsCount != 27 || *a.IndexedVectorsCount != 54 || a.Status != "green" || a.OptimizerStatus != "ok" || b.PointsCount == nil || *b.PointsCount != 0 || b.IndexedVectorsCount != nil || b.OptimizerStatus != "error" {
		t.Fatalf("counts/status lost: %+v %+v", a, b)
	}
	if !reflect.DeepEqual(paths, []string{"/", "/collections", "/collections/a", "/collections/b"}) {
		t.Fatal(paths)
	}
	raw, _ := json.Marshal(q)
	if strings.Contains(string(raw), "must-stay-local") || strings.Contains(string(raw), "secret") {
		t.Fatalf("raw metadata escaped: %s", raw)
	}
}

func TestQdrantUnavailableNeverBecomesZeroOrUnhealthy(t *testing.T) {
	for _, tc := range []struct {
		name, root, list, detail, state, reason string
		status                                  int
	}{
		{name: "auth root", status: 401, state: "auth_required"},
		{name: "forbidden root", status: 403, state: "auth_required"},
		{name: "api missing", status: 404, state: "unavailable", reason: "api"},
		{name: "wrong service", root: `{"version":"1.15.0","title":"other"}`, state: "unavailable", reason: "api"},
		{name: "malformed root", root: `{`, state: "unavailable", reason: "api"},
		{name: "incomplete list", root: qdrantRoot, list: `{"status":"ok","result":{}}`, state: "partial", reason: "api"},
		{name: "failed list", root: qdrantRoot, list: `{"status":"error","result":{"collections":[]}}`, state: "partial", reason: "api"},
		{name: "auth collection", root: qdrantRoot, list: `{"status":"ok","result":{"collections":[{"name":"a"}]}}`, status: 401, state: "auth_required"},
		{name: "null result", root: qdrantRoot, list: `{"status":"ok","result":{"collections":[{"name":"a"}]}}`, detail: `{"status":"ok","result":null}`, state: "partial", reason: "api"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			q := testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) {
				body := tc.detail
				if r.URL.Path == "/" {
					body = tc.root
				}
				if r.URL.Path == "/collections" {
					body = tc.list
				}
				if body == "" && tc.status != 0 {
					w.WriteHeader(tc.status)
					return
				}
				fmt.Fprint(w, body)
			})
			if q.State != tc.state || q.Reason != tc.reason {
				t.Fatalf("%+v", q)
			}
			if tc.list == "" || strings.Contains(tc.name, "list") {
				if q.CollectionsTotal != nil {
					t.Fatal("unknown total became zero")
				}
			}
			for _, c := range q.Collections {
				if c.PointsCount != nil || c.IndexedVectorsCount != nil || c.Status != "" {
					t.Fatalf("unknown became numeric or unhealthy: %+v", c)
				}
			}
		})
	}
}

func TestQdrantEmptyAndOlderMetadata(t *testing.T) {
	for _, tc := range []struct {
		name, list, detail string
		count              int
	}{
		{"empty", `{"status":"ok","result":{"collections":[]}}`, "", 0},
		{"older optional fields", `{"status":"ok","result":{"collections":[{"name":"old"}]}}`, `{"status":"ok","result":{"status":"green","vectors_count":23}}`, 1},
		{"negative invalid counts", `{"status":"ok","result":{"collections":[{"name":"old"}]}}`, `{"status":"ok","result":{"points_count":-1,"indexed_vectors_count":-1,"optimizer_status":"unrecognized"}}`, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			q := testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/":
					fmt.Fprint(w, qdrantRoot)
				case "/collections":
					fmt.Fprint(w, tc.list)
				default:
					fmt.Fprint(w, tc.detail)
				}
			})
			if q.State != "ok" || q.CollectionsTotal == nil || *q.CollectionsTotal != tc.count {
				t.Fatalf("%+v", q)
			}
			for _, c := range q.Collections {
				if c.PointsCount != nil || c.IndexedVectorsCount != nil || c.OptimizerStatus != "" {
					t.Fatalf("invented fields: %+v", c)
				}
			}
		})
	}
}

func TestQdrantBoundsAndPaths(t *testing.T) {
	requests := 0
	q := testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Path == "/" {
			fmt.Fprint(w, qdrantRoot)
			return
		}
		if r.URL.Path == "/collections" {
			fmt.Fprint(w, `{"status":"ok","result":{"collections":[`)
			for i := 0; i < 25; i++ {
				if i > 0 {
					fmt.Fprint(w, ",")
				}
				fmt.Fprintf(w, `{"name":"c%02d"}`, i)
			}
			fmt.Fprint(w, `]}}`)
			return
		}
		fmt.Fprint(w, `{"status":"ok","result":{"points_count":1}}`)
	})
	if requests != 2+qdrantMaxCollections || !q.Truncated || *q.CollectionsTotal != 25 || len(q.Collections) != qdrantMaxCollections {
		t.Fatalf("unbounded collection scan: %+v, requests=%d", q, requests)
	}
	q = testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			fmt.Fprint(w, qdrantRoot)
			return
		}
		if r.URL.Path == "/collections" {
			fmt.Fprint(w, `{"status":"ok","result":{"collections":[{"name":"../metrics"},{"name":"x?y#z"}]}}`)
			return
		}
		if r.URL.RawQuery != "" || !strings.HasPrefix(r.URL.EscapedPath(), "/collections/") {
			t.Error("collection escaped metadata route")
		}
		fmt.Fprint(w, `{"status":"ok","result":{"points_count":1}}`)
	})
	if q.State != "partial" || len(q.Collections) != 1 || q.Collections[0].Name != "x?y#z" {
		t.Fatalf("unsafe name handling: %+v", q)
	}
	q = testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, strings.Repeat("x", qdrantMaxBody+1)) })
	if q.State != "unavailable" || q.Reason != "limit" || q.CollectionsTotal != nil {
		t.Fatalf("oversized body: %+v", q)
	}
}

func TestQdrantTimeoutRedirectAndBudget(t *testing.T) {
	redirected := 0
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { redirected++ }))
	defer redirect.Close()
	q := testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, redirect.URL, http.StatusFound) })
	if redirected != 0 || q.Reason != "api" || q.State != "unavailable" {
		t.Fatalf("redirect followed: %+v %d", q, redirected)
	}
	q = testQdrantRead(t, func(w http.ResponseWriter, r *http.Request) { <-r.Context().Done() })
	if q.Reason != "timeout" || q.State != "unavailable" {
		t.Fatalf("timeout: %+v", q)
	}
	cl := newQdrantClient()
	defer cl.CloseIdleConnections()
	for _, budget := range []*qdrantBudget{{0, 100}, {1, 0}} {
		q = readQdrant(context.Background(), cl, "http://127.0.0.1:1", budget)
		if q.Reason != "limit" {
			t.Fatalf("budget: %+v", q)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Nanosecond)
	defer cancel()
	<-ctx.Done()
	q = readQdrant(ctx, cl, "http://127.0.0.1:1", &qdrantBudget{1, 100})
	if q.Reason != "timeout" {
		t.Fatalf("overall timeout: %+v", q)
	}
	tr := cl.Transport.(*http.Transport)
	if tr.Proxy != nil {
		t.Fatal("environment proxy can receive local metadata")
	}
}

func TestQdrantRequestAndResponseBudgetsAreConsumed(t *testing.T) {
	requests := 0
	cl := newQdrantClient()
	cl.Transport = qdrantRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"ok":true}`)), Header: make(http.Header)}, nil
	})
	b := &qdrantBudget{requests: 2, bytes: 100}
	var response map[string]any
	for i := 0; i < 2; i++ {
		if why := b.get(context.Background(), cl, "http://127.0.0.1:6333/", &response); why != "" {
			t.Fatal(why)
		}
	}
	if why := b.get(context.Background(), cl, "http://127.0.0.1:6333/", &response); why != "limit" || requests != 2 {
		t.Fatalf("request budget bypassed: %s %d", why, requests)
	}
	b = &qdrantBudget{requests: 10, bytes: 15}
	if why := b.get(context.Background(), cl, "http://127.0.0.1:6333/", &response); why != "" {
		t.Fatal(why)
	}
	if why := b.get(context.Background(), cl, "http://127.0.0.1:6333/", &response); why != "limit" {
		t.Fatalf("shared body budget bypassed: %s", why)
	}
	before := requests
	if why := b.get(context.Background(), cl, "http://127.0.0.1:6333/", &response); why != "limit" || requests != before {
		t.Fatalf("exhausted body budget issued a request: %s", why)
	}
}

func TestQdrantTargetBoundaries(t *testing.T) {
	for _, ip := range []string{"", "example.com", "http://127.0.0.1", "127.0.0.1:80", "169.254.169.254", "8.8.8.8", "0.0.0.0", "::", "ff02::1", "::ffff:127.0.0.1", "fe80::1%eth0"} {
		if qdrantEndpoint(ip) != "" {
			t.Errorf("unsafe endpoint %q", ip)
		}
	}
	for _, ip := range []string{"127.0.0.1", "10.1.2.3", "192.168.1.3", "172.17.0.2", "::1", "fd00::2"} {
		if qdrantEndpoint(ip) == "" {
			t.Errorf("local address refused %q", ip)
		}
	}
	dk := &dockerInfo{Access: true, Containers: []dockerContainer{
		{Name: "server", Image: "qdrant/qdrant:v1.15", State: "running", ip: "10.1.1.2"},
		{Name: "client", Image: "qdrant/qdrant-client:v1", State: "running", ip: "10.1.1.3"},
		{Name: "stopped", Image: "qdrant/qdrant:v1", State: "exited", ip: "10.1.1.4"},
		{Name: "other", Image: "milvusdb/milvus:v2", State: "running", ip: "10.1.1.5"},
	}}
	kube := &kubeInfo{Access: true, Pods: []kubePod{
		{NS: "ns", Name: "local", Image: "qdrant/qdrant:v1", Phase: "Running", local: true, ip: "10.1.1.6"},
		{NS: "ns", Name: "remote", Image: "qdrant/qdrant:v1", Phase: "Running", ip: "10.1.1.7"},
	}}
	targets := qdrantTargets(dk, kube, nil, t.TempDir())
	if len(targets) != 2 || targets[0].source != "контейнер server" || targets[1].source != "под ns/local" {
		t.Fatalf("bad discovery: %+v", targets)
	}
	host := qdrantTargets(&dockerInfo{Access: true, Containers: []dockerContainer{{Name: "host-net", Image: "qdrant/qdrant:v1", State: "running", hostNetwork: true}}}, nil, nil, t.TempDir())
	if len(host) != 1 || host[0].ip != "127.0.0.1" || host[0].source != "контейнер host-net" {
		t.Fatalf("host network container: %+v", host)
	}
	dk.Access, kube.Access = false, false
	if len(qdrantTargets(dk, kube, nil, t.TempDir())) != 0 {
		t.Fatal("discovery without inventory access")
	}
	unknown := collectQdrantServices(&dockerInfo{Access: true, Containers: []dockerContainer{{Name: "private", Image: "qdrant/qdrant:v1", State: "running"}}}, nil, nil, t.TempDir())
	if len(unknown) != 1 || unknown[0].Qdrant.State != "unavailable" || unknown[0].Qdrant.Reason != "address" {
		t.Fatal("empty IP fell back to host")
	}
}

func TestQdrantDockerHostNetworkComesFromReadOnlyInspect(t *testing.T) {
	cl := &http.Client{Transport: qdrantRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != "GET" || r.URL.Path != "/containers/lab/json" {
			t.Fatalf("unexpected Docker request: %s %s", r.Method, r.URL.Path)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"HostConfig":{"NetworkMode":"host"}}`))}, nil
	})}
	c := dockerContainer{Name: "lab", Image: "qdrant/qdrant:v1.15.0", State: "running"}
	dockerInspect(cl, "lab", &c)
	if !c.hostNetwork {
		t.Fatal("host network mode lost between Docker inspect and service discovery")
	}
}

func TestQdrantNativeRequiresSameNetworkNamespace(t *testing.T) {
	proc := t.TempDir()
	for _, dir := range []string{"self/net", "101/net", "102/net"} {
		if err := os.MkdirAll(filepath.Join(proc, dir), 0700); err != nil {
			t.Fatal(err)
		}
	}
	self := filepath.Join(proc, "self/net/dev")
	if err := os.WriteFile(self, []byte("net"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(self, filepath.Join(proc, "101/net/dev")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(proc, "102/net/dev"), []byte("different namespace"), 0600); err != nil {
		t.Fatal(err)
	}
	procs := map[int]procSample{101: {comm: "qdrant"}, 102: {comm: "qdrant"}, 103: {comm: "qdrant-client"}}
	targets := qdrantTargets(nil, nil, procs, proc)
	if len(targets) != 1 || targets[0].ip != "127.0.0.1" || targets[0].source != "хост" {
		t.Fatalf("native discovery: %+v", targets)
	}
	if err := os.WriteFile(filepath.Join(proc, "101/cgroup"), []byte("0::/docker/"+strings.Repeat("a", 64)), 0600); err != nil {
		t.Fatal(err)
	}
	if len(qdrantTargets(nil, nil, procs, proc)) != 0 {
		t.Fatal("container process probed as host")
	}
}

type qdrantRoundTripFunc func(*http.Request) (*http.Response, error)

func (f qdrantRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestQdrantMultiInstanceScanSharesDeadlineAndBudget(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), qdrantScanTimeout)
	defer cancel()
	requests := 0
	cl := newQdrantClient()
	cl.Transport = qdrantRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		body := qdrantRoot
		if r.URL.Path == "/collections" {
			body = `{"status":"ok","result":{"collections":[{"name":"a"},{"name":"b"},{"name":"c"},{"name":"d"},{"name":"e"},{"name":"f"},{"name":"g"},{"name":"h"}]}}`
		}
		if strings.HasPrefix(r.URL.Path, "/collections/") {
			select {
			case <-time.After(220 * time.Millisecond):
				body = `{"status":"ok","result":{"points_count":1}}`
			case <-r.Context().Done():
				return nil, r.Context().Err()
			}
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})
	targets := []qdrantTarget{{"first", "127.0.0.1"}, {"second", "127.0.0.2"}, {"third", "127.0.0.3"}, {"fourth", "127.0.0.4"}}
	start := time.Now()
	services := probeQdrantTargets(ctx, targets, cl)
	elapsed := time.Since(start)
	if elapsed > qdrantScanTimeout+150*time.Millisecond || len(services) != 4 || requests > qdrantMaxRequests || services[3].Qdrant.State != "unavailable" {
		t.Fatalf("scan did not share bounds: elapsed=%v requests=%d services=%+v", elapsed, requests, services)
	}
	t.Logf("four-instance scan: elapsed=%v requests=%d last_state=%s", elapsed, requests, services[3].Qdrant.State)
}

func TestQdrantLiveMetadata(t *testing.T) {
	if os.Getenv("KERVAX_TEST_QDRANT") != "1" {
		t.Skip("requires isolated Qdrant network namespace and neutral pre-seeded fixture")
	}
	// This optional live check only reads metadata. Fixtures are created externally
	// in a disposable container with no published ports or persistent volumes.
	services := collectQdrantServices(&dockerInfo{Access: true, Containers: []dockerContainer{{Name: "qdrant-lab", Image: "qdrant/qdrant:v1.15.0", State: "running", ip: "127.0.0.1"}}}, nil, nil, t.TempDir())
	if len(services) != 1 {
		t.Fatalf("missing live metadata: %+v", services)
	}
	q := services[0].Qdrant
	if os.Getenv("KERVAX_TEST_QDRANT_AUTH") == "1" {
		if q.State != "auth_required" || q.CollectionsTotal != nil || len(q.Collections) != 0 {
			t.Fatalf("closed API did not stay unknown: %+v", q)
		}
	} else if q.State != "ok" || q.Version != "1.15.0" || q.CollectionsTotal == nil || *q.CollectionsTotal != 1 || len(q.Collections) != 1 || q.Collections[0].Name != "kervax_metadata_probe" || q.Collections[0].PointsCount == nil || *q.Collections[0].PointsCount != 2 || q.Collections[0].IndexedVectorsCount == nil || *q.Collections[0].IndexedVectorsCount != 0 {
		t.Fatalf("live metadata incorrect: %+v", q)
	}
	if dest := os.Getenv("KERVAX_TEST_QDRANT_REPORT"); dest != "" {
		raw, err := json.MarshalIndent(struct {
			Services []serviceInfo `json:"services"`
		}{services}, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(dest, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	raw, _ := json.Marshal(services)
	t.Logf("live read-only collector: %s", raw)
}
