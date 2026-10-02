package main

// Кнопки "Освободить": агент кладет в спул только известное действие и режим, а имя
// контейнера - по белому списку символов. Путь к логу из панели не принимается вовсе.

import (
	"strings"
	"testing"
)

func TestDiskFixLines(t *testing.T) {
	ok := []backupCommand{
		{Name: "journal", Mode: "preview"},
		{Name: "docker-build-cache", Mode: "run"},
		{Name: "container-log", Mode: "run", Container: "coolify-sentinel"},
	}
	for _, c := range ok {
		lines, good := diskFixLines(c)
		if !good || lines[0] != "action="+c.Name || lines[1] != "mode="+c.Mode {
			t.Fatalf("%+v: ждали запрос, получили %v %v", c, lines, good)
		}
	}
	lines, _ := diskFixLines(ok[2])
	if strings.Join(lines, "|") != "action=container-log|mode=run|container=coolify-sentinel" {
		t.Fatalf("лог контейнера: %v", lines)
	}
	bad := []backupCommand{
		{Name: "rm-rf", Mode: "run"},
		{Name: "journal", Mode: "wipe"},
		{Name: "container-log", Mode: "run"},
		{Name: "container-log", Mode: "run", Container: "../../etc/passwd"},
		{Name: "container-log", Mode: "run", Container: "-rf"},
		{Name: "container-log", Mode: "run", Container: "a b"},
	}
	for _, c := range bad {
		if _, good := diskFixLines(c); good {
			t.Fatalf("%+v: такой запрос не должен уйти в спул", c)
		}
	}
}
