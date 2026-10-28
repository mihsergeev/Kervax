package main

import (
	"reflect"
	"strings"
	"testing"
)

const testMirrorKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE1B7YqQzg7XNLJ2sx2hrF9SolyUDQG5d6BuG9qxn2rh"

// Основному серверу бэкапов: ключ резервного, его адрес и репозитории; пустой список убирает доступ
func TestMirrorAllowLines(t *testing.T) {
	got, ok := backupServerLines(backupCommand{Action: "mirror_allow", Name: "mirror-a", ClientIP: "203.0.113.7",
		PubKey: testMirrorKey, Repos: []string{"web-a", "db.b"}})
	want := []string{"action=mirror_allow", "name=mirror-a", "client_ip=203.0.113.7", "pubkey=" + testMirrorKey,
		"repo=web-a", "repo=db.b"}
	if !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q %v, want %q", got, ok, want)
	}
	got, ok = backupServerLines(backupCommand{Action: "mirror_allow", Name: "mirror-a"})
	if !ok || !reflect.DeepEqual(got, []string{"action=mirror_allow", "name=mirror-a"}) {
		t.Fatalf("revoke: got %q %v", got, ok)
	}
	for _, bad := range []backupCommand{
		{Action: "mirror_allow", Name: "../x", ClientIP: "203.0.113.7", PubKey: testMirrorKey, Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: testMirrorKey, Repos: []string{"../a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: "ssh-rsa AAAA", Repos: []string{"a"}},
		// опции authorized_keys или второй ключ не должны пройти вместе с ключом
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: `command="sh" ` + testMirrorKey, Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: testMirrorKey + "\nssh-ed25519 x", Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "example.com", PubKey: testMirrorKey, Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "", PubKey: testMirrorKey, Repos: []string{"a"}},
	} {
		if got, ok := backupServerLines(bad); ok {
			t.Errorf("%+v: must be refused, got %q", bad, got)
		}
	}
}

// Резервному: по строке на основной сервер, или "забрать сейчас"; кривое не уходит в спул
func TestMirrorLines(t *testing.T) {
	hk := "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyExampleOnlyForTestsAAAAAAAAAAAA"
	got, ok := mirrorLines(backupCommand{Mode: "set", Sources: []mirrorSource{
		{Name: "bk-01", Host: "203.0.113.5", Port: 22, HostKey: hk, Repos: []string{"web-a", "db-b"}},
		{Name: "bk-02", Host: "backup.example.com", Port: 2222, HostKey: "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY=", Repos: nil},
	}})
	want := []string{"action=set",
		"source=bk-01 203.0.113.5 22 " + hk + " web-a,db-b",
		"source=bk-02 backup.example.com 2222 ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= -"}
	if !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q %v, want %q", got, ok, want)
	}
	if got, ok := mirrorLines(backupCommand{Mode: "pull"}); !ok || !reflect.DeepEqual(got, []string{"action=pull"}) {
		t.Fatalf("pull: got %q %v", got, ok)
	}
	src := mirrorSource{Name: "bk-01", Host: "203.0.113.5", Port: 22, HostKey: hk, Repos: []string{"a"}}
	mod := func(f func(*mirrorSource)) backupCommand {
		s := src
		f(&s)
		return backupCommand{Mode: "set", Sources: []mirrorSource{s}}
	}
	for _, bad := range []backupCommand{
		{Mode: "drop"},
		{Mode: "set", Sources: []mirrorSource{src, src}},
		mod(func(s *mirrorSource) { s.Name = "-x" }),
		mod(func(s *mirrorSource) { s.Host = "203.0.113.5 22" }),
		mod(func(s *mirrorSource) { s.Host = "-oProxyCommand=x" }),
		mod(func(s *mirrorSource) { s.Port = 0 }),
		mod(func(s *mirrorSource) { s.Port = 70000 }),
		mod(func(s *mirrorSource) { s.HostKey = "ssh-dss AAAAB3NzaC1kc3MAAACBAP" }),
		mod(func(s *mirrorSource) { s.HostKey = hk + " extra" }),
		mod(func(s *mirrorSource) { s.Repos = []string{"a,b"} }),
		mod(func(s *mirrorSource) { s.Repos = []string{"a b"} }),
	} {
		if got, ok := mirrorLines(bad); ok {
			t.Errorf("%+v: must be refused, got %q", bad, got)
		}
	}
	// перевод строки нигде не проходит: он дописал бы в запрос свои ключи
	for _, l := range want {
		if strings.ContainsAny(l, "\r\n") {
			t.Fatalf("newline in %q", l)
		}
	}
}
