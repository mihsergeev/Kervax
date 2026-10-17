package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func ioWrite(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestCgroupIO(t *testing.T) {
	names := map[string]string{"259:0": "nvme0n1", "259:1": "nvme1n1", "9:43": "md43", "7:0": "loop0"}
	dev := func(mm string) string { return names[mm] }
	d := t.TempDir()
	// RAID1 над двумя NVMe: считается md, иначе запись удвоилась бы; loop - не диск
	ioWrite(t, d+"/raid/io.stat", "259:0 rbytes=100 wbytes=2000 rios=1 wios=2 dbytes=0 dios=0\n"+
		"259:1 rbytes=50 wbytes=2000 rios=1 wios=2 dbytes=0 dios=0\n"+
		"9:43 rbytes=150 wbytes=2000 rios=2 wios=2 dbytes=0 dios=0\n"+
		"7:0 rbytes=9999 wbytes=9999 rios=1 wios=1 dbytes=0 dios=0\n")
	if r, w, v1, ok := cgroupIO(d+"/raid", dev); !ok || v1 || r != 150 || w != 2000 {
		t.Fatalf("raid: %d %d %v %v", r, w, v1, ok)
	}
	// без RAID - сумма по дискам, незнакомое устройство тоже диск
	ioWrite(t, d+"/plain/io.stat", "259:0 rbytes=10 wbytes=20\n8:16 rbytes=1 wbytes=2\n")
	if r, w, _, _ := cgroupIO(d+"/plain", dev); r != 11 || w != 22 {
		t.Fatalf("plain: %d %d", r, w)
	}
	// cgroup v1
	ioWrite(t, d+"/v1/blkio.throttle.io_service_bytes", "259:0 Read 300\n259:0 Write 700\n259:0 Sync 1000\n259:0 Total 1000\nTotal 1000\n")
	if r, w, v1, ok := cgroupIO(d+"/v1", dev); !ok || !v1 || r != 300 || w != 700 {
		t.Fatalf("v1: %d %d %v %v", r, w, v1, ok)
	}
	if _, _, _, ok := cgroupIO(d+"/none", dev); ok {
		t.Fatal("no cgroup files must not be ok")
	}
}

func TestDiskWriters(t *testing.T) {
	ioState.Lock()
	ioState.hist, ioState.last, ioState.top = map[string][]ioSample{}, time.Time{}, nil
	ioState.Unlock()
	root := t.TempDir()
	id := strings.Repeat("ab", 32)
	uid := "56279ecf-da54-4be1-ab7e-f3774036245e"
	podDir := root + "/kubepods/besteffort/pod" + uid
	set := func(c, u, p, s uint64) {
		ioWrite(t, root+"/system.slice/docker-"+id+".scope/io.stat", "259:0 rbytes=0 wbytes="+itoa(c)+"\n")
		ioWrite(t, root+"/system.slice/postgresql.service/io.stat", "259:0 rbytes=5 wbytes="+itoa(u)+"\n")
		ioWrite(t, podDir+"/io.stat", "259:0 rbytes=0 wbytes="+itoa(p)+"\n")
		ioWrite(t, root+"/user.slice/io.stat", "259:0 rbytes=0 wbytes="+itoa(s)+"\n")
	}
	conts := map[string]string{id: "feed-app", "dead": ""}
	pods := map[string]string{uid: "shop/web-0"}
	t0 := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	set(1000, 1000, 1000, 1000)
	if w, _ := diskWriters(root, conts, pods, t0); len(w) != 0 {
		t.Fatalf("first sweep has nothing to compare with: %+v", w)
	}
	set(5000, 2000, 1000, 1500)
	// раньше минуты - прошлый результат, без нового замера
	if w, _ := diskWriters(root, conts, pods, t0.Add(30*time.Second)); len(w) != 0 {
		t.Fatalf("within ioStep: %+v", w)
	}
	w, v1 := diskWriters(root, conts, pods, t0.Add(10*time.Minute))
	if v1 || len(w) != 3 {
		t.Fatalf("%v %+v", v1, w)
	}
	if w[0] != (ioWriter{Kind: "container", Name: "feed-app", W: 4000, R: 0, S: 600}) ||
		w[1].Name != "postgresql.service" || w[1].W != 1000 || w[2].Name != "user.slice" {
		t.Fatalf("%+v", w)
	}
	// контейнер пересоздан: счетчики с нуля - история заново, без отрицательных разниц
	set(10, 2500, 1000, 1500)
	w, _ = diskWriters(root, conts, pods, t0.Add(20*time.Minute))
	if len(w) != 2 || w[0].Name != "postgresql.service" || w[0].W != 1500 || w[0].S != 1200 || w[1].Name != "user.slice" {
		t.Fatalf("after reset: %+v", w)
	}
	// окно не длиннее ioKeep: замер старше 65 минут выпадает, база - замер 60 минут назад
	set(10, 9000, 1000, 1500)
	w, _ = diskWriters(root, conts, pods, t0.Add(70*time.Minute))
	if len(w) != 1 || w[0].Name != "postgresql.service" || w[0].S != 3600 || w[0].W != 9000-2000 {
		t.Fatalf("window: %+v", w)
	}
	// удаленная cgroup забывается
	if err := os.RemoveAll(podDir); err != nil {
		t.Fatal(err)
	}
	diskWriters(root, conts, pods, t0.Add(72*time.Minute))
	ioState.Lock()
	_, kept := ioState.hist["p:"+uid]
	ioState.Unlock()
	if kept {
		t.Fatal("history of a removed pod must be dropped")
	}
}

func itoa(n uint64) string {
	b := []byte{}
	for {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
		if n == 0 {
			return string(b)
		}
	}
}
