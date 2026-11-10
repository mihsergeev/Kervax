package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestPanelRedirectDoesNotSendAuthenticatedReportToAnotherOrigin(t *testing.T) {
	const token = "Bearer synthetic-agent-token"
	const payload = "synthetic-report"
	sink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer sink.Close()
	panel := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/external":
			http.Redirect(w, r, sink.URL, http.StatusTemporaryRedirect)
		case "/internal":
			http.Redirect(w, r, "/report", http.StatusTemporaryRedirect)
		case "/report":
			body, err := io.ReadAll(r.Body)
			if err != nil || string(body) != payload || r.Header.Get("Authorization") != token {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer panel.Close()

	client := panelClient(5 * time.Second)
	for _, tc := range []struct {
		name string
		path string
		auth string
		want int
	}{
		{"authenticated cross-origin report", "/external", token, http.StatusTemporaryRedirect},
		{"authenticated same-origin report", "/internal", token, http.StatusNoContent},
		{"unauthenticated download", "/external", "", http.StatusNoContent},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req, err := http.NewRequest(http.MethodPost, panel.URL+tc.path, strings.NewReader(payload))
			if err != nil {
				t.Fatal(err)
			}
			if tc.auth != "" {
				req.Header.Set("Authorization", tc.auth)
			}
			resp, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != tc.want {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.want)
			}
		})
	}
}
