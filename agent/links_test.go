package main

import (
	"net"
	"strconv"
	"testing"
)

func TestLinkValid(t *testing.T) {
	for _, ok := range []linkTask{{1, "10.0.0.5", 5432}, {2, "db.example.com", 443}, {3, "2001:db8::1", 22}} {
		if !linkValid(ok) {
			t.Fatalf("должен пропустить %+v", ok)
		}
	}
	for _, bad := range []linkTask{{0, "a", 1}, {1, "", 1}, {1, "a", 0}, {1, "a", 70000}, {1, "a b", 1},
		{1, "http://x", 80}, {1, "u@h", 22}} {
		if linkValid(bad) {
			t.Fatalf("пропустил %+v", bad)
		}
	}
}

func TestProbeAndCollectLinks(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	open := ln.Addr().(*net.TCPAddr).Port
	// закрытый порт: слушаем и сразу закрываем
	l2, _ := net.Listen("tcp", "127.0.0.1:0")
	closed := l2.Addr().(*net.TCPAddr).Port
	l2.Close()

	known := map[int]bool{1: true, 2: true}
	linkKnown.Store(&known)
	probeLink(linkTask{1, "127.0.0.1", open})
	probeLink(linkTask{2, "127.0.0.1", closed})
	probeLink(linkTask{3, "127.0.0.1", open}) // панель про него уже не знает
	got := collectLinks()
	if len(got) != 2 {
		t.Fatalf("got %+v", got)
	}
	if got[0].ID != 1 || got[0].Sent != 3 || got[0].OK != 3 || got[0].Err != "" || got[0].RttMs <= 0 {
		t.Fatalf("open: %+v", got[0])
	}
	if got[1].ID != 2 || got[1].OK != 0 || got[1].Err != "connection refused" {
		t.Fatalf("closed %s: %+v", strconv.Itoa(closed), got[1])
	}
	// после отчета накопленное обнулилось
	if again := collectLinks(); len(again) != 0 {
		t.Fatalf("again: %+v", again)
	}
}
