package main

import (
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestProcNetAddr(t *testing.T) {
	ip, port, ok := procNetAddr("0100007F:0035")
	if !ok || ip.String() != "127.0.0.1" || port != 53 {
		t.Fatalf("v4: %v %d %v", ip, port, ok)
	}
	ip, port, ok = procNetAddr("00000000000000000000000000000000:1F90")
	if !ok || ip.String() != "::" || port != 8080 {
		t.Fatalf("v6 any: %v %d %v", ip, port, ok)
	}
	// 2001:db8::1 в порядке ядра: каждое 32-битное слово little-endian
	ip, _, ok = procNetAddr("B80D0120000000000000000001000000:01BB")
	if !ok || ip.String() != "2001:db8::1" {
		t.Fatalf("v6: %v %v", ip, ok)
	}
	if _, _, ok := procNetAddr("zz:0035"); ok {
		t.Fatal("bad hex accepted")
	}
}

func TestParseListen(t *testing.T) {
	tcp := `  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1538 00000000:0000 0A 00000000:00000000 00:00000000 00000000   113        0 1 1 0000000000000000 100 0 0 10 0
   1: 0100007F:0CEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 2 1 0000000000000000 100 0 0 10 0
   2: 0A00000A:0016 0A000014:C350 01 00000000:00000000 00:00000000 00000000     0        0 3 1 0000000000000000 100 0 0 10 0
   3: 00000000:1538 00000000:0000 0A 00000000:00000000 00:00000000 00000000   113        0 4 1 0000000000000000 100 0 0 10 0
`
	udp := `  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  10: 00000000:CA6C 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 5 2 0000000000000000 0
  11: 0A00000A:A1B2 08080808:0035 01 00000000:00000000 00:00000000 00000000     0        0 6 2 0000000000000000 0
`
	users := map[string]string{"113": "postgres"}
	seen := map[string]bool{}
	got := parseListen("tcp", tcp, users, seen, nil)
	got = parseListen("udp", udp, users, seen, got)
	// Postgres на всех адресах (один раз, хотя строк две), localhost и установленное
	// соединение не в счет; UDP - только несвязанный сокет (WireGuard на 51820)
	want := []listenSock{{"tcp", "0.0.0.0", 5432, "postgres"}, {"udp", "0.0.0.0", 51820, "0"}}
	if len(got) != len(want) {
		t.Fatalf("got %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%d: got %+v want %+v", i, got[i], want[i])
		}
	}
}

func TestPairCountersAndConntrack(t *testing.T) {
	snmp := "Ip: Forwarding DefaultTTL\nIp: 1 64\nTcp: RtoAlgorithm OutSegs RetransSegs InErrs\nTcp: 1 1000 25 3\n"
	m := pairCounters(snmp, "Tcp")
	if m["OutSegs"] != 1000 || m["RetransSegs"] != 25 || m["InErrs"] != 3 {
		t.Fatalf("snmp: %v", m)
	}
	ct := "entries searched found new invalid ignore delete delete_list insert insert_failed drop early_drop\n" +
		"00000010 00000000 00000000 00000000 00000005 00000000 00000000 00000000 00000000 00000002 00000001 00000000\n" +
		"00000010 00000000 00000000 00000000 00000005 00000000 00000000 00000000 00000000 00000000 0000000a 00000001\n"
	n, ok := conntrackDrops(ct)
	if !ok || n != 2+1+10+1 {
		t.Fatalf("conntrack: %d %v", n, ok)
	}
}

func TestTCPRate(t *testing.T) {
	prev := tcpCtr{ok: true, okExt: true, okCt: true, out: 1000, retrans: 10, listenOvf: 5, ctDrops: 0, synRetrans: 4}
	cur := tcpCtr{ok: true, okExt: true, okCt: true, out: 2000, retrans: 60, listenOvf: 35, ctDrops: 20, synRetrans: 44}
	r := tcpRate(cur, prev, 10)
	if r == nil || r.Out != 100 || r.Retrans != 5 || r.ListenOvf != 3 || r.CtDrop != 2 || r.SynRetrans != 4 {
		t.Fatalf("rate: %+v", r)
	}
	// счетчик сбросился (перезагрузка) - не отрицательная скорость
	if r := tcpRate(tcpCtr{ok: true, out: 5}, tcpCtr{ok: true, out: 100}, 10); r.Out != 0 {
		t.Fatalf("reset: %+v", r)
	}
	if tcpRate(tcpCtr{}, prev, 10) != nil {
		t.Fatal("no source must give nil")
	}
}

func TestVerCmpAndUpdates(t *testing.T) {
	if verCmp("6.8.0-47-generic", "6.8.0-45-generic") <= 0 || verCmp("5.15.0-9", "5.15.0-10") >= 0 ||
		verCmp("6.8.0-45-generic", "6.8.0-45-generic") != 0 {
		t.Fatal("verCmp")
	}
	upd, sec, ok := parseUpdatesAvailable("\n12 updates can be applied immediately.\n5 of these updates are standard security updates.\nTo see these additional updates run: apt list --upgradable\n")
	if !ok || upd != 12 || sec != 5 {
		t.Fatalf("en: %d %d %v", upd, sec, ok)
	}
	upd, sec, ok = parseUpdatesAvailable("7 обновлений может быть применено немедленно.\n3 из этих обновлений, являются стандартными обновлениями безопасности.\n")
	if !ok || upd != 7 || sec != 3 {
		t.Fatalf("ru: %d %d %v", upd, sec, ok)
	}
	if _, _, ok := parseUpdatesAvailable("\n"); ok {
		t.Fatal("empty text")
	}
}

func TestDockerPortsAndDstate(t *testing.T) {
	ps := []struct {
		IP          string
		PrivatePort int
		PublicPort  int
		Type        string
	}{{"0.0.0.0", 5432, 5432, "tcp"}, {"::", 5432, 5432, "tcp"}, {"", 80, 0, "tcp"}, {"0.0.0.0", 5432, 5432, "tcp"}}
	got := dockerPorts(ps)
	if len(got) != 2 || got[0].Pub != 5432 || got[1].IP != "::" {
		t.Fatalf("ports: %+v", got)
	}
	d := dstate(map[int]procSample{1: {comm: "nfsd", state: "D"}, 2: {comm: "nfsd", state: "D"},
		3: {comm: "jbd2", state: "D"}, 4: {comm: "bash", state: "S"}})
	if d.N != 3 || len(d.Top) != 2 || d.Top[0] != (dstateProc{"nfsd", 2}) {
		t.Fatalf("dstate: %+v", d)
	}
}

// TestLiveDump печатает новые замеры на живой системе: KVX_LIVE=1 go test -run TestLiveDump
func TestLiveDump(t *testing.T) {
	if os.Getenv("KVX_LIVE") == "" {
		t.Skip("KVX_LIVE не задан")
	}
	b, _ := json.MarshalIndent(map[string]any{
		"listen": collectListen(), "ips": publicIPs(), "psi": readPSI(), "fds": readFds(),
		"dstate": dstate(readProcs()), "tcp": readTCP().out, "os_upd": collectOSUpdates(time.Now()),
	}, "", " ")
	t.Log(string(b))
}
