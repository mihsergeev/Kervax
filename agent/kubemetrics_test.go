package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestParseQuantity(t *testing.T) {
	cases := map[string]float64{
		"250m": 0.25, "67258n": 0.000067258, "43296Ki": 44335104, "1k": 1000, "32": 32,
		"1.5Gi": 1.5 * (1 << 30), "195329988Ki": 195329988 * 1024, "1e3": 1000,
	}
	for in, want := range cases {
		got, ok := parseQuantity(in)
		if !ok || got < want*0.999999 || got > want*1.000001 {
			t.Errorf("%s: %v %v, want %v", in, got, ok, want)
		}
	}
	for _, bad := range []string{"", "abc", "-1", "Ki"} {
		if _, ok := parseQuantity(bad); ok {
			t.Errorf("%q must be refused", bad)
		}
	}
}

func TestKubeMetrics(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/apis/metrics.k8s.io/v1beta1/pods":
			w.Write([]byte(`{"items":[{"metadata":{"name":"access-hub-5965758d6c-tbvbr","namespace":"access-hub"},
				"containers":[{"name":"a","usage":{"cpu":"67258n","memory":"43296Ki"}},
				              {"name":"b","usage":{"cpu":"250m","memory":"1Mi"}}]}]}`))
		case "/apis/metrics.k8s.io/v1beta1/nodes":
			w.Write([]byte(`{"items":[{"metadata":{"name":"k8s-a-prc"},"usage":{"cpu":"2500m","memory":"8Gi"}}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	pods, nodes := kubeMetrics(http.DefaultClient, &kubeConf{Server: srv.URL, Token: "x"})
	u := pods["access-hub/access-hub-5965758d6c-tbvbr"]
	if u == nil || u.CPUm != 250 || u.Mem != 43296*1024+1<<20 {
		t.Fatalf("pod: %+v", u)
	}
	if n := nodes["k8s-a-prc"]; n == nil || n.CPUm != 2500 || n.Mem != 8<<30 {
		t.Fatalf("node: %+v", n)
	}
	// нет metrics-server: оба nil, без ошибки
	none := httptest.NewServer(http.NotFoundHandler())
	defer none.Close()
	if p, n := kubeMetrics(http.DefaultClient, &kubeConf{Server: none.URL, Token: "x"}); p != nil || n != nil {
		t.Fatal("без metrics-server должно быть пусто")
	}
	if c := quantityUse("32", "195329988Ki"); c == nil || c.CPUm != 32000 || c.Mem != 195329988*1024 {
		t.Fatalf("capacity: %+v", c)
	}
}
