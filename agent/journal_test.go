package main

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"strings"
	"testing"
)

func TestUnitJournalLines(t *testing.T) {
	got, ok := unitJournalLines(backupCommand{Name: "nginx.service", Since: 60, Lines: 200, Prio: "err"})
	want := []string{"op=journal", "unit=nginx.service", "since=60", "lines=200", "prio=err"}
	if !ok || strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("got %v", got)
	}
	// весь журнал и ядро - без имени юнита; числа зажимаются, приоритет по умолчанию info
	got, ok = unitJournalLines(backupCommand{Name: "_kernel", Since: 999999, Lines: 5, Prio: "debug"})
	if !ok || got[2] != "since=10080" || got[3] != "lines=10" || got[4] != "prio=info" {
		t.Fatalf("got %v", got)
	}
	if got, ok = unitJournalLines(backupCommand{Name: "_all"}); !ok || got[2] != "since=1" || got[3] != "lines=300" {
		t.Fatalf("got %v", got)
	}
	for _, bad := range []string{"", "nginx", "-u.service", "a;b.service", "x.target", "../x.service"} {
		if _, ok := unitJournalLines(backupCommand{Name: bad}); ok {
			t.Fatalf("пропустил %q", bad)
		}
	}
}

func TestUnpackJournalAndTail(t *testing.T) {
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	zw.Write([]byte("2026-10-14T12:00:00+0300 host nginx[1]: started\n"))
	zw.Close()
	text, err := unpackJournal(base64.StdEncoding.EncodeToString(buf.Bytes()) + "\n")
	if err != nil || !strings.Contains(text, "nginx[1]: started") {
		t.Fatalf("%q %v", text, err)
	}
	if _, err := unpackJournal("не base64"); err == nil {
		t.Fatal("мусор должен дать ошибку")
	}
	long := strings.Repeat("старая строка\n", 50) + "свежая\n"
	tail := tailText(long, 40)
	if !strings.HasSuffix(tail, "свежая\n") || len(tail) > 40 || strings.HasPrefix(tail, "\n") {
		t.Fatalf("%q", tail)
	}
	if tailText("коротко", 100) != "коротко" {
		t.Fatal("короткий текст не трогаем")
	}
}
