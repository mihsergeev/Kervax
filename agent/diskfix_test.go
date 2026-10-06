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

// Разобрать раздел ниже 75% (прогноз обещает заполнение): только run и только путь раздела по
// белому списку символов. Свой ли это раздел, helper сверит с df еще раз.
func TestDiskAnalyzeLines(t *testing.T) {
	lines, good := diskFixLines(backupCommand{Name: "analyze", Mode: "run", Mount: "/app"})
	if !good || strings.Join(lines, "|") != "action=analyze|mode=run|mount=/app" {
		t.Fatalf("разбор /app: %v %v", lines, good)
	}
	if _, good := diskFixLines(backupCommand{Name: "analyze", Mode: "run", Mount: "/"}); !good {
		t.Fatalf("корень тоже раздел")
	}
	for _, c := range []backupCommand{
		{Name: "analyze", Mode: "preview", Mount: "/app"},
		{Name: "analyze", Mode: "run"},
		{Name: "analyze", Mode: "run", Mount: "app"},
		{Name: "analyze", Mode: "run", Mount: "/app/../etc"},
		{Name: "analyze", Mode: "run", Mount: "/app\nmode=run"},
		{Name: "analyze", Mode: "run", Mount: "/my disk"},
		{Name: "analyze", Mode: "run", Mount: "//app"},
	} {
		if _, good := diskFixLines(c); good {
			t.Fatalf("%+v: такой разбор не должен уйти в спул", c)
		}
	}
}
