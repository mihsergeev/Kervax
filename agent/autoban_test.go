package main

import (
	"reflect"
	"testing"
)

// Запрос helper'у autoban-setup: режим, разбан и белый список; кривое не уходит в спул
func TestAutobanLines(t *testing.T) {
	cases := []struct {
		cmd  backupCommand
		want []string
	}{
		{backupCommand{Mode: "on"}, []string{"op=mode", "mode=on"}},
		{backupCommand{Mode: "watch"}, []string{"op=mode", "mode=watch"}},
		{backupCommand{Mode: "unban", Name: "203.0.113.9"}, []string{"op=unban", "ip=203.0.113.9"}},
		{backupCommand{Mode: "unban", Name: "2001:DB8::5"}, []string{"op=unban", "ip=2001:db8::5"}},
		{backupCommand{Mode: "allow", Paths: []string{"203.0.113.0/24 офис", "2001:db8::/48", "198.51.100.7  Мой \"VPN\"\\"}},
			[]string{"op=allow", "net=203.0.113.0/24 офис", "net=2001:db8::/48", "net=198.51.100.7 Мой VPN"}},
		{backupCommand{Mode: "allow"}, []string{"op=allow"}},
	}
	for _, c := range cases {
		got, ok := autobanLines(c.cmd)
		if !ok || !reflect.DeepEqual(got, c.want) {
			t.Errorf("%+v: got %q %v, want %q", c.cmd, got, ok, c.want)
		}
	}
	for _, bad := range []backupCommand{
		{Mode: "ban", Name: "203.0.113.9"},
		{Mode: "unban", Name: "203.0.113.9; rm -rf /"},
		{Mode: "unban", Name: ""},
		{Mode: "allow", Paths: []string{"example.com"}},
		{Mode: "allow", Paths: []string{"203.0.113.0/33"}},
		{Mode: ""},
	} {
		if got, ok := autobanLines(bad); ok {
			t.Errorf("%+v: must be refused, got %q", bad, got)
		}
	}
	// перевод строки в подписи дописал бы в запрос свои ключи
	got, ok := autobanLines(backupCommand{Mode: "allow", Paths: []string{"203.0.113.1 a\nop=mode\nmode=off"}})
	if !ok || len(got) != 2 || got[1] != "net=203.0.113.1 aop=modemode=off" {
		t.Errorf("newline in label: %q %v", got, ok)
	}
}
