package main

import (
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeDNS - UDP-резолвер на 127.0.0.1: отвечает на каждый запрос через delay кодом rcode
// (0 - с A-записью 192.0.2.1). Возвращает адрес ip:port.
func fakeDNS(t *testing.T, delay time.Duration, rcode byte) string {
	t.Helper()
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pc.Close() })
	go func() {
		buf := make([]byte, 1500)
		for {
			n, addr, err := pc.ReadFrom(buf)
			if err != nil {
				return
			}
			q := append([]byte(nil), buf[:n]...)
			go func(q []byte, addr net.Addr) {
				time.Sleep(delay)
				// конец вопроса: метки до нулевого байта, потом тип и класс
				end := 12
				for end < len(q) && q[end] != 0 {
					end += int(q[end]) + 1
				}
				end += 5
				if end > len(q) {
					return
				}
				an := byte(0)
				if rcode == 0 {
					an = 1
				}
				resp := []byte{q[0], q[1], 0x81, 0x80 | rcode, 0, 1, 0, an, 0, 0, 0, 0}
				resp = append(resp, q[12:end]...)
				if rcode == 0 {
					resp = append(resp, 0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 192, 0, 2, 1)
				}
				_, _ = pc.WriteTo(resp, addr)
			}(q, addr)
		}
	}()
	return pc.LocalAddr().String()
}

func TestDNSAskMeasuresEachAnswer(t *testing.T) {
	fast := fakeDNS(t, 0, 0)
	if ms, e := dnsAsk(fast, "panel.example.", time.Second); e != "" || ms < 0 || ms > 500 {
		t.Fatalf("fast answer: ms=%d err=%q", ms, e)
	}
	slow := fakeDNS(t, 300*time.Millisecond, 0)
	if ms, e := dnsAsk(slow, "panel.example.", time.Second); e != "" || ms < 250 {
		t.Fatalf("slow answer must show its time: ms=%d err=%q", ms, e)
	}
	if ms, e := dnsAsk(fakeDNS(t, 0, 3), "kvx-1.panel.example.", time.Second); e != "" || ms < 0 {
		t.Fatalf("NXDOMAIN is an answer: ms=%d err=%q", ms, e)
	}
	if ms, e := dnsAsk(fakeDNS(t, 0, 2), "panel.example.", time.Second); e != "servfail" || ms != -1 {
		t.Fatalf("SERVFAIL: ms=%d err=%q", ms, e)
	}
	if ms, e := dnsAsk(fakeDNS(t, 3*time.Second, 0), "panel.example.", 400*time.Millisecond); e != "timeout" || ms != -1 {
		t.Fatalf("no answer in time: ms=%d err=%q", ms, e)
	}
}

func TestDNSServersFromResolvConf(t *testing.T) {
	dir := t.TempDir()
	write := func(name, body string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	upstream := write("upstream", "# resolved\nnameserver 185.12.64.2\nnameserver 185.12.64.1\nnameserver 2a01:4ff:ff00::add:1\n")
	stub := write("stub", "nameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n")
	if mode, s := dnsServersFrom(stub, upstream); mode != "resolved" || len(s) != 2 || s[0] != "185.12.64.2" || s[1] != "185.12.64.1" {
		t.Fatalf("systemd-resolved: mode=%s servers=%v", mode, s)
	}
	if mode, s := dnsServersFrom(write("local", "nameserver 127.0.0.1\n"), upstream); mode != "local" || len(s) != 1 {
		t.Fatalf("own unbound: mode=%s servers=%v", mode, s)
	}
	direct := write("direct", "nameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 9.9.9.9\nnameserver 1.0.0.1\nnameserver 8.8.4.4\n")
	if mode, s := dnsServersFrom(direct, upstream); mode != "direct" || len(s) != 4 {
		t.Fatalf("direct, at most 4: mode=%s servers=%v", mode, s)
	}
	if mode, s := dnsServersFrom(filepath.Join(dir, "missing"), upstream); mode != "direct" || len(s) != 0 {
		t.Fatalf("no resolv.conf: mode=%s servers=%v", mode, s)
	}
}
