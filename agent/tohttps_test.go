package main

import "testing"

func TestToHTTPSLines(t *testing.T) {
	ok := backupCommand{Action: "to_https", Port: 64101, CacertB64: "LS0tLS1CRUdJTg=="}
	lines, good := toHTTPSLines(ok)
	if !good || len(lines) != 3 || lines[0] != "action=to_https" || lines[1] != "port=64101" ||
		lines[2] != "cacert_b64=LS0tLS1CRUdJTg==" {
		t.Fatalf("lines %v %v", lines, good)
	}
	for _, bad := range []backupCommand{
		{Port: 0, CacertB64: "AAAA"},
		{Port: 70000, CacertB64: "AAAA"},
		{Port: 64101},
		{Port: 64101, CacertB64: "AAAA\naction=deprovision"},
	} {
		if _, good := toHTTPSLines(bad); good {
			t.Fatalf("accepted %+v", bad)
		}
	}
}

func TestAllowTLSLines(t *testing.T) {
	lines, good := backupServerLines(backupCommand{Action: "allow_tls", ClientIP: "203.0.113.5"})
	if !good || len(lines) != 2 || lines[1] != "client_ip=203.0.113.5" {
		t.Fatalf("lines %v %v", lines, good)
	}
	if _, good := backupServerLines(backupCommand{Action: "allow_tls", ClientIP: "1.2.3.4\naction=x"}); good {
		t.Fatal("accepted a broken address")
	}
}
