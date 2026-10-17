package main

import (
	"os"
	"strings"
	"testing"
	"time"
)

// netDev - net/dev в формате ядра: две строки шапки, lo и интерфейсы
func netDev(ifs ...[3]string) string {
	s := "Inter-|   Receive                                                |  Transmit\n" +
		" face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n" +
		"    lo: 777777    10    0    0    0     0          0         0   777777    10    0    0    0     0       0          0\n"
	for _, f := range ifs {
		s += "  " + f[0] + ": " + f[1] + " 5 0 0 0 0 0 0 " + f[2] + " 5 0 0 0 0 0 0\n"
	}
	return s
}

func TestNetDevBytes(t *testing.T) {
	d := t.TempDir()
	ioWrite(t, d+"/dev", netDev([3]string{"eth0", "1000", "200"}, [3]string{"eth1", "30", "4"}))
	// lo не в счет, два интерфейса складываются
	if rx, tx, ok := netDevBytes(d + "/dev"); !ok || rx != 1030 || tx != 204 {
		t.Fatalf("%d %d %v", rx, tx, ok)
	}
	if _, _, ok := netDevBytes(d + "/none"); ok {
		t.Fatal("missing file must not be ok")
	}
}

func TestNetUsers(t *testing.T) {
	netState.Lock()
	netState.hist, netState.last, netState.top = map[string][]ioSample{}, time.Time{}, nil
	netState.Unlock()
	root, proc := t.TempDir(), t.TempDir()
	app, side, hostc, idle := strings.Repeat("a1", 32), strings.Repeat("b2", 32), strings.Repeat("c3", 32), strings.Repeat("d4", 32)
	uid := "56279ecf-da54-4be1-ab7e-f3774036245e"
	scope := func(id string) string { return root + "/system.slice/docker-" + id + ".scope" }
	ioWrite(t, scope(app)+"/cgroup.procs", "100\n101\n")
	ioWrite(t, scope(side)+"/cgroup.procs", "200\n")
	ioWrite(t, scope(hostc)+"/cgroup.procs", "300\n")
	ioWrite(t, scope(idle)+"/cgroup.procs", "")
	// у пода на v2 процессов в его каталоге нет, они в каталогах контейнеров
	pod := root + "/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod" + strings.ReplaceAll(uid, "-", "_") + ".slice"
	ioWrite(t, pod+"/cgroup.procs", "")
	ioWrite(t, pod+"/cri-containerd-0f0f.scope/cgroup.procs", "400\n")
	set := func(app, pod [2]string, host string) {
		ioWrite(t, proc+"/100/net/dev", netDev([3]string{"eth0", app[0], app[1]}))
		ioWrite(t, proc+"/400/net/dev", netDev([3]string{"eth0", pod[0], pod[1]}))
		ioWrite(t, proc+"/self/net/dev", netDev([3]string{"ens3", host, host}))
	}
	set([2]string{"1000", "1000"}, [2]string{"50", "50"}, "999999999")
	// sidecar в пространстве app (network_mode: container:app), контейнер с сетью хоста - как агент
	for _, l := range [][2]string{{"/100/net/dev", "/200/net/dev"}, {"/self/net/dev", "/300/net/dev"}} {
		if err := os.MkdirAll(proc+l[1][:4]+"/net", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.Link(proc+l[0], proc+l[1]); err != nil {
			t.Fatal(err)
		}
	}
	conts := map[string]string{app: "shop-app", side: "shop-sidecar", hostc: "node-exporter", idle: "stopped"}
	pods := map[string]string{uid: "shop/web-0"}
	t0 := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	if u := netUsers(root, proc, conts, pods, t0); len(u) != 0 {
		t.Fatalf("first sweep has nothing to compare with: %+v", u)
	}
	set([2]string{"91000", "5001000"}, [2]string{"3050", "150"}, "1999999999")
	if u := netUsers(root, proc, conts, pods, t0.Add(30*time.Second)); len(u) != 0 {
		t.Fatalf("within ioStep: %+v", u)
	}
	u := netUsers(root, proc, conts, pods, t0.Add(10*time.Minute))
	if len(u) != 2 {
		t.Fatalf("%+v", u)
	}
	if u[0] != (netUser{Kind: "container", Name: "shop-app, shop-sidecar", Rx: 90000, Tx: 5000000, S: 600}) ||
		u[1] != (netUser{Kind: "pod", Name: "shop/web-0", Rx: 3000, Tx: 100, S: 600}) {
		t.Fatalf("%+v", u)
	}
	// контейнер пересоздан: счетчики с нуля - история заново, сравнивать не с чем
	set([2]string{"10", "10"}, [2]string{"4050", "150"}, "2999999999")
	u = netUsers(root, proc, conts, pods, t0.Add(20*time.Minute))
	if len(u) != 1 || u[0].Name != "shop/web-0" || u[0].Rx != 4000 || u[0].S != 1200 {
		t.Fatalf("%+v", u)
	}
	// без своего net/dev не отличить сеть хоста - ничего
	if err := os.Remove(proc + "/self/net/dev"); err != nil {
		t.Fatal(err)
	}
	if u := netUsers(root, proc, conts, pods, t0.Add(30*time.Minute)); u != nil {
		t.Fatalf("%+v", u)
	}
}

func TestNetTop(t *testing.T) {
	var l []netUser
	for i := 0; i < 12; i++ {
		rx := uint64(1)
		if i < 8 {
			rx = uint64(500 - i)
		}
		l = append(l, netUser{Kind: "container", Name: "c" + itoa(uint64(i)), Rx: rx, Tx: uint64(1000 - i)})
	}
	// много принял, мало отдал: по исходящему не в восьмерке, но в списке есть
	l[11].Rx = 1 << 30
	top := netTop(l, 8)
	if len(top) != 9 || top[0].Name != "c11" || top[8].Name != "c7" {
		t.Fatalf("%+v", top)
	}
	if got := netTop(l[:3], 8); len(got) != 3 || got[0].Name != "c0" {
		t.Fatalf("%+v", got)
	}
}
