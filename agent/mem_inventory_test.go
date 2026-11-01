package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestPodInventoryCompleteness(t *testing.T) {
	for _, tc := range []struct {
		name          string
		count, status int
		more          string
		complete      bool
	}{
		{"empty", 0, 200, "", true}, {"missing_items", -1, 200, "", false}, {"null_items", -2, 200, "", false}, {"one", 1, 200, "", true}, {"failed", 0, 503, "", false},
		{"continued", 1, 200, "next", false}, {"locally_capped", 401, 200, "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/api/v1/pods" {
					http.NotFound(w, r)
					return
				}
				w.WriteHeader(tc.status)
				if tc.count < 0 {
					if tc.count == -1 {
						fmt.Fprint(w, `{}`)
					} else {
						fmt.Fprint(w, `{"items":null}`)
					}
					return
				}
				items := make([]any, tc.count)
				for i := range items {
					items[i] = map[string]any{"metadata": map[string]any{"name": fmt.Sprintf("sample-%d", i), "namespace": "sample"}, "status": map[string]any{"phase": "Running"}}
				}
				json.NewEncoder(w).Encode(map[string]any{"metadata": map[string]string{"continue": tc.more}, "items": items})
			}))
			defer srv.Close()
			_, complete := kubePods(srv.Client(), &kubeConf{Server: srv.URL})
			if complete != tc.complete {
				t.Fatalf("complete=%v want %v", complete, tc.complete)
			}
		})
	}
}

func TestNodeMemorySampleKeepsTimestampAndRequiresMemory(t *testing.T) {
	ts := time.Now().UTC().Truncate(time.Second)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/apis/metrics.k8s.io/v1beta1/nodes" {
			fmt.Fprint(w, `{"items":[]}`)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"items": []any{
			map[string]any{"metadata": map[string]string{"name": "worker-a"}, "timestamp": ts, "usage": map[string]string{"memory": "16Mi", "cpu": "1m"}},
			map[string]any{"metadata": map[string]string{"name": "worker-b"}, "timestamp": ts, "usage": map[string]string{"cpu": "1m"}},
		}})
	}))
	defer srv.Close()
	_, nodes := kubeMetrics(srv.Client(), &kubeConf{Server: srv.URL})
	if nodes["worker-a"] == nil || nodes["worker-a"].Ts != ts.Unix() || nodes["worker-a"].Mem != 16<<20 {
		t.Fatalf("invalid sample: %#v", nodes["worker-a"])
	}
	if _, ok := nodes["worker-b"]; ok {
		t.Fatal("missing memory became a zero-byte sample")
	}
}
