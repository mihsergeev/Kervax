package main

import (
	"strconv"
	"testing"
	"time"
)

func TestMemLimits(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root+"/cgroup.controllers", "cpu memory io\n")
	mib := uint64(1 << 20)
	u := func(v uint64) string { return strconv.FormatUint(v, 10) + "\n" }
	// под k0s (cgroupfs): лимит у пода и у контейнера, контейнер ближе к лимиту, бьется о него
	pod := root + "/kubepods/burstable/pod386fef04-0000-1111-2222-333344445555"
	writeFile(t, pod+"/memory.max", u(400*mib))
	writeFile(t, pod+"/memory.current", u(390*mib))
	writeFile(t, pod+"/memory.stat", "anon 1\n")
	c := pod + "/18f0e5e51e67e052afb1700231c011cade8766f114374fc14a3889a44fcb2f8a"
	writeFile(t, c+"/memory.max", u(384*mib))
	writeFile(t, c+"/memory.current", u(383*mib))
	writeFile(t, c+"/memory.stat", "anon 380000000\nfile 2000000\npgmajfault 1000\nworkingset_refault_file 5000\nworkingset_refault_anon 0\n")
	writeFile(t, c+"/memory.pressure", "some avg10=3.10 avg60=2.79 avg300=1.00 total=10\nfull avg10=5.25 avg60=2.78 avg300=0.90 total=9\n")
	// pause-контейнер без лимита и под далеко от лимита - не в отчете
	writeFile(t, pod+"/pause1234567890/memory.max", "max\n")
	writeFile(t, pod+"/pause1234567890/memory.current", "1000\n")
	far := root + "/kubepods/burstable/pod11111111-2222-3333-4444-555555555555"
	writeFile(t, far+"/memory.max", u(1024*mib))
	writeFile(t, far+"/memory.current", u(100*mib))
	// docker с systemd-драйвером: кэш у лимита, давления нет
	dk := root + "/system.slice/docker-59911f21a52cef9d1778875c213b2bdb7c6f12b48c7971cbc4905865b7cff8af.scope"
	writeFile(t, dk+"/memory.max", u(256*mib))
	writeFile(t, dk+"/memory.current", u(250*mib))
	writeFile(t, dk+"/memory.stat", "anon 50000000\nfile 200000000\n")
	writeFile(t, dk+"/memory.pressure", "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
	// юнит с MemoryHigh ниже MemoryMax: лимит - memory.high
	un := root + "/system.slice/php-fpm.service"
	writeFile(t, un+"/memory.max", "max\n")
	writeFile(t, un+"/memory.high", u(100*mib))
	writeFile(t, un+"/memory.current", u(99*mib))
	// юнит без лимита
	writeFile(t, root+"/system.slice/cron.service/memory.max", "max\n")
	writeFile(t, root+"/system.slice/cron.service/memory.current", "100\n")

	now := time.Unix(1_800_000_000, 0)
	conts := map[string]string{"59911f21a52cef9d1778875c213b2bdb7c6f12b48c7971cbc4905865b7cff8af": "web"}
	pods := map[string]string{"386fef04-0000-1111-2222-333344445555": "default/db-mariadb-0"}
	got := memLimits(root, conts, pods, now)
	if len(got) != 3 {
		t.Fatalf("ждали под, контейнер и юнит: %+v", got)
	}
	p := got[0]
	if p.Kind != "pod" || p.Name != "default/db-mariadb-0" || p.U != "386fef04" || p.Lim != 384*mib || p.Full != 2.78 ||
		p.F10 != 5.25 || p.Some != 2.79 || p.Anon != 380000000 || p.MF != 0 {
		t.Fatalf("под: %+v", p)
	}
	// второй замер через 30 секунд: скорость faults и refault
	writeFile(t, c+"/memory.stat", "anon 380000000\nfile 2000000\npgmajfault 4000\nworkingset_refault_file 35000\nworkingset_refault_anon 0\n")
	got = memLimits(root, conts, pods, now.Add(30*time.Second))
	if got[0].MF != 100 || got[0].RF != 1000 {
		t.Fatalf("скорость: %+v", got[0])
	}
	var names []string
	for _, m := range got[1:] {
		names = append(names, m.Kind+":"+m.Name)
		if m.Kind == "unit" && m.Lim != 100*mib {
			t.Fatalf("юнит: лимит из memory.high: %+v", m)
		}
	}
	if len(names) != 2 || !(names[0] == "container:web" || names[1] == "container:web") {
		t.Fatalf("остальные: %v", names)
	}
}

func TestMemLimitsV1(t *testing.T) {
	// cgroup v1: давления нет, список пустой, но не nil (поле в отчете есть)
	got := memLimits(t.TempDir(), nil, nil, time.Now())
	if got == nil || len(got) != 0 {
		t.Fatalf("%v", got)
	}
}
