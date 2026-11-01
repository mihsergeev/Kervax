package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestAlertRecheckRejectsArbitraryCollectors(t *testing.T) {
	if _, err := collectAlertRecheck(alertRecheckTask{ID: "012345678901234567890123456789ab", Source: "run-now"}); err == nil {
		t.Fatal("write action accepted")
	}
	if _, err := rootRecheck(alertRecheckTask{ID: "../request", Source: "changes"}); err == nil {
		t.Fatal("unsafe request id accepted")
	}
}

func TestAlertRecheckReceiptMustMatchTheRequest(t *testing.T) {
	oldRoot := recheckRoot
	recheckRoot = t.TempDir()
	defer func() { recheckRoot = oldRoot }()
	for _, p := range []string{"recheck-req", "recheck-res", "recheck-work"} {
		os.Mkdir(filepath.Join(recheckRoot, p), 0700)
	}
	task := alertRecheckTask{ID: "012345678901234567890123456789ab", Source: "changes"}
	done := make(chan error, 1)
	go func() { _, err := rootRecheck(task); done <- err }()
	p := filepath.Join(recheckRoot, "recheck-req", task.ID+".json")
	for i := 0; i < 100; i++ {
		if b, err := os.ReadFile(p); err == nil {
			var req alertRecheckTask
			if json.Unmarshal(b, &req) != nil || req.ID != task.ID {
				t.Fatal("request published before complete JSON")
			}
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	os.WriteFile(filepath.Join(recheckRoot, "recheck-res", task.ID+".json"), []byte(`{"id":"another","ok":true,"snapshot":{"extras":{}}}`), 0600)
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("wrong nonce accepted")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("receipt not read")
	}
}

func TestAlertRecheckMemoryIsANewObservation(t *testing.T) {
	snap, err := collectAlertRecheck(alertRecheckTask{Source: "mem"})
	if err != nil {
		t.Fatal(err)
	}
	var total uint64
	if json.Unmarshal(snap["mem_total"], &total) != nil || total == 0 {
		t.Fatal("no fresh memory inventory")
	}
}

func TestAlertRecheckPublishedAfterInFlightReport(t *testing.T) {
	reportEntered, releaseReport, resultEntered := make(chan struct{}), make(chan struct{}), make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/agent/report" {
			close(reportEntered)
			<-releaseReport
		} else if r.URL.Path == "/api/agent/alert-recheck-result" {
			close(resultEntered)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	normalDone := make(chan struct{})
	go func() { collectAndSend(srv.URL, "neutral-test-token", snap()); close(normalDone) }()
	select {
	case <-reportEntered:
	case <-time.After(30 * time.Second):
		t.Fatal("normal report did not start")
	}
	manualDone := make(chan struct{})
	go func() {
		runAlertRecheck(srv.URL, "neutral-test-token", alertRecheckTask{ID: "abcdef0123456789abcdef0123456789", Source: "mem"})
		close(manualDone)
	}()
	select {
	case <-resultEntered:
		close(releaseReport)
		t.Fatal("fresh result overtook an older report")
	case <-time.After(200 * time.Millisecond):
	}
	close(releaseReport)
	select {
	case <-manualDone:
	case <-time.After(10 * time.Second):
		t.Fatal("result did not follow acknowledged report")
	}
	<-normalDone
	select {
	case <-resultEntered:
	default:
		t.Fatal("no fresh result")
	}
}
