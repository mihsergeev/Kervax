package main

import (
	"encoding/base64"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestKubeRequestsKeepCredentialsAtAPIOrigin(t *testing.T) {
	const token = "synthetic-service-account-token"
	var outsideRequests atomic.Int32
	outside := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		outsideRequests.Add(1)
		_, _ = w.Write([]byte("{}"))
	}))
	defer outside.Close()

	api := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+token {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch {
		case r.URL.Path == "/version":
			http.Redirect(w, r, "/version/real", http.StatusTemporaryRedirect)
		case r.URL.Path == "/version/real":
			_, _ = w.Write([]byte(`{"gitVersion":"v1.30.0"}`))
		case strings.HasSuffix(r.URL.Path, "/pods/demo"):
			_, _ = w.Write([]byte("{}"))
		default:
			http.Redirect(w, r, outside.URL, http.StatusTemporaryRedirect)
		}
	}))
	defer api.Close()

	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: api.Certificate().Raw})
	conf := &kubeConf{Server: api.URL, Token: token, CA: base64.StdEncoding.EncodeToString(ca)}
	client := kubeClient(conf)
	var version map[string]string
	if err := kubeGet(client, conf, "/version", &version); err != nil || version["gitVersion"] != "v1.30.0" {
		t.Fatalf("same-origin version request failed: %v, %v", version, err)
	}
	if err := kubeGet(client, conf, "/redirect", &version); err == nil {
		t.Error("redirected read succeeded")
	}
	if ok, _ := kubeWrite(client, conf, http.MethodPatch, "/redirect", "{}", "application/json"); ok {
		t.Error("redirected write succeeded")
	}
	if ok, _ := kubePodLogs(client, conf, kubeCommand{Action: "logs", NS: "default", Name: "demo"}); ok {
		t.Error("redirected logs request succeeded")
	}
	if count := outsideRequests.Load(); count != 0 {
		t.Fatalf("cross-origin requests reached the destination: %d", count)
	}
}
