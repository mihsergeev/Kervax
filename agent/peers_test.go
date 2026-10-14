package main

import "testing"

const ssOne = `0      0      203.0.113.10:22      198.51.100.20:51432 users:(("sshd",pid=812,fd=4))
	 cubic wscale:7,7 rto:420 rtt:210.5/20.1 mss:1288 cwnd:10 bytes_sent:9000000 retrans:0/100 rcv_space:14600
0      0      203.0.113.10:22      198.51.100.20:51433 users:(("sshd",pid=813,fd=4))
	 cubic rto:204 rtt:3.1/1 mss:1448 cwnd:10
0      0      [::ffff:203.0.113.10]:443 [::ffff:198.51.100.7]:6000 users:(("caddy",pid=90,fd=12))
	 cubic rto:204 rtt:30/2 retrans:1/40
0      0      [2001:db8::1]:443   [2001:db8::9]:7000
	 cubic retrans:0/5
`

const ssTwo = `0      0      203.0.113.10:22      198.51.100.20:51432 users:(("sshd",pid=812,fd=4))
	 cubic rto:420 retrans:2/400 rcv_space:14600
0      0      203.0.113.10:22      198.51.100.20:51433 users:(("sshd",pid=813,fd=4))
	 cubic rto:204 retrans:0/7
0      0      [::ffff:203.0.113.10]:443 [::ffff:198.51.100.7]:6000 users:(("caddy",pid=90,fd=12))
	 cubic retrans:0/50
0      0      203.0.113.10:443      192.0.2.5:9000 users:(("caddy",pid=90,fd=13))
	 cubic retrans:0/900
`

func TestParseSSRetrans(t *testing.T) {
	r := parseSSRetrans(ssOne)
	k := "203.0.113.10:22|198.51.100.20:51432"
	if r.totals[k] != 100 || r.peer[k] != "198.51.100.20" || r.port[k] != 22 || r.proc[k] != "sshd" {
		t.Fatalf("ssh: %v %v %v %v", r.totals[k], r.peer[k], r.port[k], r.proc[k])
	}
	if r.totals["203.0.113.10:22|198.51.100.20:51433"] != 0 {
		t.Fatal("без retrans - ноль")
	}
	k6 := "[::ffff:203.0.113.10]:443|[::ffff:198.51.100.7]:6000"
	if r.peer[k6] != "198.51.100.7" || r.port[k6] != 443 || r.totals[k6] != 40 {
		t.Fatalf("v4 в v6: %+v", r)
	}
	if r.peer["[2001:db8::1]:443|[2001:db8::9]:7000"] != "2001:db8::9" {
		t.Fatalf("v6: %+v", r.peer)
	}
}

func TestAggregatePeers(t *testing.T) {
	prev := parseSSRetrans(ssOne).totals
	peers, total := aggregatePeers(prev, parseSSRetrans(ssTwo))
	// новое соединение к 192.0.2.5 не считаем: его 900 повторов могли накопиться когда угодно
	if total != 300+7+10 || len(peers) != 2 {
		t.Fatalf("total=%d peers=%+v", total, peers)
	}
	p := peers[0]
	if p.IP != "198.51.100.20" || p.Retrans != 307 || p.Socks != 2 || p.Proc != "sshd" || p.Port != 22 {
		t.Fatalf("top: %+v", p)
	}
	if peers[1].IP != "198.51.100.7" || peers[1].Retrans != 10 || peers[1].Proc != "caddy" {
		t.Fatalf("second: %+v", peers[1])
	}
}

func TestSplitHostPort(t *testing.T) {
	for in, want := range map[string]string{"1.2.3.4:443": "1.2.3.4", "[2001:db8::1]:22": "2001:db8::1",
		"[::ffff:10.0.0.1]:80": "10.0.0.1", "*:22": "", "[fe80::1%eth0]:22": "fe80::1", "bad": ""} {
		if got, _ := splitHostPort(in); got != want {
			t.Fatalf("%s: %q", in, got)
		}
	}
	if retransPeersGate(&tcpRates{Out: 1000, Retrans: 20}) || !retransPeersGate(&tcpRates{Out: 1000, Retrans: 100, SynRetrans: 10}) ||
		retransPeersGate(&tcpRates{Out: 10, Retrans: 9}) || retransPeersGate(nil) {
		t.Fatal("порог просмотра")
	}
}
