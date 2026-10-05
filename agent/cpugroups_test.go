package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestProcOwner(t *testing.T) {
	id := strings.Repeat("ab", 32)
	uid := "56279ecf-da54-4be1-ab7e-f3774036245e"
	cases := []struct{ cg, kind, id string }{
		{"0::/system.slice/docker-" + id + ".scope\n", "container", id},
		{"0::/docker/" + id + "\n", "container", id},
		// k0s: cgroupfs, под важнее контейнера внутри него
		{"0::/kubepods/besteffort/pod" + uid + "/" + id + "\n", "pod", uid},
		{"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod" +
			strings.ReplaceAll(uid, "-", "_") + ".slice/cri-containerd-" + id + ".scope\n", "pod", uid},
		{"0::/system.slice/nginx.service\n", "unit", "nginx.service"},
		{"0::/user.slice/user-0.slice/session-1.scope\n", "", ""},
		// cgroup v1: несколько строк, путь берется из той, где он не корень
		{"12:pids:/docker/" + id + "\n11:memory:/docker/" + id + "\n0::/\n", "container", id},
		{"", "", ""},
	}
	for _, c := range cases {
		if k, i := procOwner(c.cg); k != c.kind || i != c.id {
			t.Errorf("%q: %q %q, want %q %q", c.cg, k, i, c.kind, c.id)
		}
	}
}

func TestCPUGroups(t *testing.T) {
	uid := "56279ecf-da54-4be1-ab7e-f3774036245e"
	pod := "0::/kubepods/besteffort/pod" + uid + "/c1\n"
	cgs := map[int]string{101: pod, 102: pod, 103: pod, 104: pod, 200: "0::/system.slice/nginx.service\n"}
	oldCg, oldUp := procCgroup, procUptime
	procCgroup = func(pid int) string { return cgs[pid] }
	procUptime = func() float64 { return 10 * 86400 } // десять дней с загрузки
	defer func() { procCgroup, procUptime = oldCg, oldUp }()

	t0 := time.Now()
	prev := sample{at: t0, procs: map[int]procSample{}}
	cur := sample{at: t0.Add(10 * time.Second), procs: map[int]procSample{}}
	put := func(pid int, comm string, startSec, busy float64, cpuPct float64) {
		start := uint64(startSec * clkTck)
		age := 10*86400 - startSec
		ticks := uint64(age * busy * clkTck)
		d := uint64(cpuPct / 100 * 10 * clkTck) // за 10 с замера
		prev.procs[pid] = procSample{comm: comm, ticks: ticks - d, start: start}
		cur.procs[pid] = procSample{comm: comm, ticks: ticks, start: start}
	}
	// три старых chrome: стартовали через час после загрузки и всю жизнь крутят ядро
	put(101, "chrome", 3600, 1.0, 100)
	put(102, "chrome", 3600, 1.0, 100)
	put(103, "chrome", 3600, 1.0, 100)
	// молодой chrome того же пода: ест ядро, но живет час - это еще не "крутится"
	put(104, "chrome", 10*86400-3600, 1.0, 100)
	put(200, "nginx", 3600, 0.01, 30)
	put(300, "idle", 3600, 0.01, 0)
	put(301, "tiny", 3600, 0.01, 0.5)
	// переиспользованный pid: в прошлом замере был другой процесс
	put(400, "reused", 3600, 1.0, 100)
	cur.procs[400] = procSample{comm: "reused", ticks: cur.procs[400].ticks, start: 999}

	got := cpuGroups(prev, cur, nil, map[string]string{uid: "ads-com/ads-retargeting-monitor-c56fcfcd7-cgrtn"})
	if len(got) != 2 {
		t.Fatalf("групп %d: %+v", len(got), got)
	}
	ch := got[0]
	if ch.Comm != "chrome" || ch.Kind != "pod" || ch.Owner != "ads-com/ads-retargeting-monitor-c56fcfcd7-cgrtn" ||
		ch.N != 4 || ch.CPU != 400 || ch.Spin != 3 || ch.SpinAge < 9*86400 {
		t.Fatalf("chrome: %+v", ch)
	}
	if ng := got[1]; ng.Comm != "nginx" || ng.Kind != "unit" || ng.Owner != "nginx.service" || ng.Spin != 0 || ng.CPU != 30 {
		t.Fatalf("nginx: %+v", ng)
	}
}

func TestCPURate(t *testing.T) {
	now := time.Now()
	if r := cpuRate("t:x", 1_000_000, now); r != 0 {
		t.Fatalf("первый замер должен быть 0, а не %v", r)
	}
	if r := cpuRate("t:x", 2_500_000, now.Add(time.Second)); r != 150 {
		t.Fatalf("1.5 с за секунду - это 150%%, а не %v", r)
	}
	// контейнер перезапустили - счетчик начался заново: не отрицательное число, а 0
	if r := cpuRate("t:x", 100, now.Add(2*time.Second)); r != 0 {
		t.Fatalf("сброс счетчика: %v", r)
	}
}

func TestCgroupCPUPaths(t *testing.T) {
	root := t.TempDir()
	id := strings.Repeat("cd", 32)
	uid := "56279ecf-da54-4be1-ab7e-f3774036245e"
	write := func(rel, body string) {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("system.slice/docker-"+id+".scope/cpu.stat", "usage_usec 123456\nuser_usec 100000\n")
	if v, ok := containerCPU(root, id); !ok || v != 123456 {
		t.Fatalf("v2 systemd: %v %v", v, ok)
	}
	id1 := strings.Repeat("ef", 32)
	write("cpuacct/docker/"+id1+"/cpuacct.usage", "5000000\n")
	if v, ok := containerCPU(root, id1); !ok || v != 5000 {
		t.Fatalf("v1: %v %v", v, ok)
	}
	if _, ok := containerCPU(root, "../etc"); ok {
		t.Fatal("путь из id не должен выходить за cgroup")
	}
	write("kubepods/besteffort/pod"+uid+"/cpu.stat", "usage_usec 777\n")
	if m := podCPU(root); m[uid] != 777 {
		t.Fatalf("pod v2: %+v", m)
	}
}
