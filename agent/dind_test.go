package main

import (
	"os"
	"strconv"
	"strings"
	"testing"
)

// Docker внутри privileged-контейнера: dockerd и shim в cgroup контейнера, у CI-агента с
// пустым dind - только dockerd, у privileged без docker - ничего. Процессы хоста не в счет.
func TestDindScan(t *testing.T) {
	root := t.TempDir()
	proc, cg := root+"/proc", root+"/cg"
	ci, idle, plain := strings.Repeat("a1", 32), strings.Repeat("b2", 32), strings.Repeat("c3", 32)
	itoa := strconv.Itoa
	stat := func(comm string, utime, cutime, start int) string {
		// поля после comm: state(3) ... utime(14) stime(15) cutime(16) cstime(17) ... starttime(22)
		return "1 (" + comm + ") S 1 1 1 0 -1 0 0 0 0 0 " + itoa(utime) + " 0 " + itoa(cutime) + " 0 20 0 9 0 " + itoa(start) + " 0 0\n"
	}
	writeFile(t, proc+"/100/cgroup", "0::/system.slice/docker-"+ci+".scope/init\n")
	writeFile(t, proc+"/100/stat", stat("dockerd", 30000, 12000, 100000))
	writeFile(t, proc+"/101/cgroup", "0::/system.slice/docker-"+ci+".scope/init\n")
	writeFile(t, proc+"/200/cgroup", "0::/system.slice/docker-"+idle+".scope\n")
	writeFile(t, proc+"/200/stat", stat("dockerd", 150, 0, 500000))
	writeFile(t, proc+"/300/cgroup", "0::/system.slice/docker.service\n")
	writeFile(t, proc+"/301/cgroup", "0::/system.slice/containerd.service\n")
	if err := os.MkdirAll(cg+"/system.slice/docker-"+ci+".scope/docker", 0o755); err != nil {
		t.Fatal(err)
	}
	procs := map[int]procSample{
		100: {comm: "dockerd", start: 100000}, 101: {comm: "containerd-shim"},
		200: {comm: "dockerd", start: 500000}, 300: {comm: "dockerd"}, 301: {comm: "containerd-shim"},
	}
	cs := []dockerContainer{
		{Name: "ci-agent", State: "running", Priv: true, id: ci},
		{Name: "ci-agent-2", State: "running", Priv: true, id: idle},
		{Name: "tool", State: "running", Priv: true, id: plain},
		{Name: "web", State: "running", id: "dd"},
		{Name: "old", State: "exited", Priv: true, id: "ee"},
	}
	dindScan(cs, procs, proc, cg, 10000)
	if d := cs[0].Dind; d == nil || *d != (dindInfo{Dockerd: true, Up: 9000, CPU: 420, Run: 1, Ran: true}) {
		t.Errorf("ci-agent: %+v", d)
	}
	if d := cs[1].Dind; d == nil || *d != (dindInfo{Dockerd: true, Up: 5000, CPU: 1}) {
		t.Errorf("ci-agent-2: %+v", d)
	}
	if d := cs[2].Dind; d == nil || *d != (dindInfo{}) {
		t.Errorf("tool: %+v", d)
	}
	if cs[3].Dind != nil || cs[4].Dind != nil {
		t.Errorf("не privileged или не работает: %+v %+v", cs[3].Dind, cs[4].Dind)
	}
}
