package main

import (
	"testing"
	"time"
)

func TestProbeKillOf(t *testing.T) {
	fin := time.Date(2026, 10, 20, 12, 45, 11, 0, time.UTC)
	ev := []kubeEvent{
		{Reason: "Unhealthy", Message: "Liveness probe failed: Get \"https://10.0.0.1:9141/livez\": context deadline exceeded", LastTimestamp: "2026-10-20T12:44:30Z"},
		{Reason: "Unhealthy", Message: "Liveness probe failed: Get \"https://10.0.0.1:9141/livez\": net/http: request canceled", LastTimestamp: "2026-10-20T12:44:50Z"},
		{Reason: "Unhealthy", Message: "Readiness probe failed: Get \"http://127.0.0.1:7572/readyz\": timeout", LastTimestamp: "2026-10-20T12:44:55Z"},
		{Reason: "Killing", Message: "Container app failed liveness probe, will be restarted", LastTimestamp: "2026-10-20T12:44:51Z"},
	}
	p, m := probeKillOf(ev, "app", fin)
	if p != "liveness" || m != "Get \"https://10.0.0.1:9141/livez\": net/http: request canceled" {
		t.Fatalf("got %q %q", p, m)
	}
	// убит другой контейнер пода - не наш случай
	if p, _ := probeKillOf(ev, "sidecar", fin); p != "" {
		t.Fatalf("other container: %q", p)
	}
	// приказ был задолго до этого завершения - это прошлый раз
	if p, _ := probeKillOf(ev, "app", fin.Add(time.Hour)); p != "" {
		t.Fatalf("old kill: %q", p)
	}
	// время только в eventTime (новые события)
	ev2 := []kubeEvent{{Reason: "Killing", Message: "Container app failed startup probe, will be restarted", EventTime: "2026-10-20T12:45:00.123456Z"}}
	if p, m := probeKillOf(ev2, "app", fin); p != "startup" || m != "" {
		t.Fatalf("startup: %q %q", p, m)
	}
}

func TestPrevLogLine(t *testing.T) {
	logs := "2026-10-20T12:44:10Z\tINFO\tsynced 35 secrets\n" +
		"I1020 12:44:53.307528 1 leaderelection.go:297] failed to renew lease default/app-lock: context deadline exceeded\n" +
		"2026-10-20T12:44:54Z\tERROR\tsetup\tproblem running manager\t{\"error\": \"leader election lost\"}\n" +
		"main.main\n" +
		"\t/workspace/cmd/main.go:376\n" +
		"runtime.main\n" +
		"\t/usr/local/go/src/runtime/proc.go:285\n"
	if got := prevLogLine(logs); got != "2026-10-20T12:44:54Z ERROR setup problem running manager {\"error\": \"leader election lost\"}" {
		t.Fatalf("got %q", got)
	}
	// ничего похожего на ошибку - последняя строка
	if got := prevLogLine("starting\nlistening on :8080\n\n"); got != "listening on :8080" {
		t.Fatalf("got %q", got)
	}
	// стек Go после panic: путь к panic.go - не причина
	panicLog := "panic: runtime error: invalid memory address\n\ngoroutine 1 [running]:\nmain.run()\n\t/src/runtime/panic.go:12 +0x1d\n"
	if got := prevLogLine(panicLog); got != "panic: runtime error: invalid memory address" {
		t.Fatalf("got %q", got)
	}
	if prevLogLine("") != "" {
		t.Fatal("empty")
	}
}
