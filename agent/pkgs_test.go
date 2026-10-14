package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseDpkgStatus(t *testing.T) {
	st := `Package: openssl
Status: install ok installed
Architecture: amd64
Version: 3.0.2-0ubuntu1.18

Package: libc6
Status: install ok installed
Architecture: amd64
Version: 2.35-0ubuntu3.8

Package: libc6
Status: install ok installed
Architecture: i386
Version: 2.35-0ubuntu3.8

Package: old-thing
Status: deinstall ok config-files
Version: 1.0

Package: held
Status: hold ok installed
Version: 2.0
`
	got := parseDpkgStatus([]byte(st))
	want := [][2]string{{"openssl", "3.0.2-0ubuntu1.18"}, {"libc6", "2.35-0ubuntu3.8"}, {"held", "2.0"}}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}

func TestParseApkAndRpm(t *testing.T) {
	apk := parseApkInstalled([]byte("C:abc\nP:musl\nV:1.2.4-r2\n\nP:busybox\nV:1.36.1-r5\n"))
	if len(apk) != 2 || apk[0] != [2]string{"musl", "1.2.4-r2"} {
		t.Fatalf("apk %v", apk)
	}
	rpm := parseRpmQa([]byte("openssl\t1:3.0.7-27.el9\nbash\t0:5.1.8-9.el9\ngpg-pubkey\t0:abc-def\n\n"))
	if len(rpm) != 2 || rpm[0] != [2]string{"openssl", "1:3.0.7-27.el9"} || rpm[1] != [2]string{"bash", "5.1.8-9.el9"} {
		t.Fatalf("rpm %v", rpm)
	}
}

func TestPkgHashStable(t *testing.T) {
	a := pkgHash([][2]string{{"a", "1"}, {"b", "2"}})
	if a != pkgHash([][2]string{{"a", "1"}, {"b", "2"}}) || a == pkgHash([][2]string{{"a", "1"}, {"b", "3"}}) || len(a) != 16 {
		t.Fatal("hash")
	}
}

func TestParseDpkgLogLine(t *testing.T) {
	loc := time.FixedZone("MSK", 3*3600)
	ev, ok := parseDpkgLogLine("2026-10-14 12:03:11 upgrade openssl:amd64 3.0.2-0ubuntu1.15 3.0.2-0ubuntu1.18", loc)
	if !ok || ev.A != "upgrade" || ev.P != "openssl" || ev.O != "3.0.2-0ubuntu1.15" || ev.N != "3.0.2-0ubuntu1.18" {
		t.Fatalf("%+v", ev)
	}
	if ev.T != time.Date(2026, 10, 14, 9, 3, 11, 0, time.UTC).Unix() {
		t.Fatalf("время в журнале местное: %d", ev.T)
	}
	ev, ok = parseDpkgLogLine("2026-10-14 12:03:12 install libfoo:amd64 <none> 1.2-3", loc)
	if !ok || ev.O != "" || ev.N != "1.2-3" {
		t.Fatalf("%+v", ev)
	}
	ev, ok = parseDpkgLogLine("2026-10-14 12:03:13 remove libbar:amd64 4.5 <none>", loc)
	if !ok || ev.A != "remove" || ev.N != "" {
		t.Fatalf("%+v", ev)
	}
	for _, l := range []string{
		"2026-10-14 12:03:11 status installed openssl:amd64 3.0.2-0ubuntu1.18",
		"2026-10-14 12:03:11 configure openssl:amd64 3.0.2-0ubuntu1.18 <none>",
		"2026-10-14 12:03:11 startup packages configure",
		"",
	} {
		if _, ok := parseDpkgLogLine(l, loc); ok {
			t.Fatalf("не событие: %q", l)
		}
	}
}

func TestDpkgTail(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "dpkg.log")
	write := func(s string, flag int) {
		f, err := os.OpenFile(p, flag|os.O_WRONLY|os.O_CREATE, 0o644)
		if err != nil {
			t.Fatal(err)
		}
		f.WriteString(s)
		f.Close()
	}
	write("2020-01-01 00:00:00 install old:amd64 <none> 1\n2026-10-14 12:00:00 install a:amd64 <none> 1\n", os.O_TRUNC)
	var dt dpkgTail
	since := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC).Unix()
	got := dt.next(p, since, time.UTC)
	if len(got) != 1 || got[0].P != "a" {
		t.Fatalf("старое отсеяно по since: %+v", got)
	}
	// недописанная строка ждет конца
	write("2026-10-14 12:01:00 upgrade b:amd64 1 2\n2026-10-14 12:02:00 remove c:am", os.O_APPEND)
	got = dt.next(p, since, time.UTC)
	if len(got) != 1 || got[0].P != "b" {
		t.Fatalf("%+v", got)
	}
	write("d64 3 <none>\n", os.O_APPEND)
	got = dt.next(p, since, time.UTC)
	if len(got) != 1 || got[0].P != "c" || got[0].A != "remove" {
		t.Fatalf("%+v", got)
	}
	// ротация: новый файл короче прочитанного - читаем с начала
	write("2026-10-15 01:00:00 install e:amd64 <none> 5\n", os.O_TRUNC)
	got = dt.next(p, since, time.UTC)
	if len(got) != 1 || got[0].P != "e" {
		t.Fatalf("после ротации: %+v", got)
	}
	if got := dt.next(p, since, time.UTC); got != nil {
		t.Fatalf("ничего нового: %+v", got)
	}
}

func TestPkgEvQueue(t *testing.T) {
	pkgEvMu.Lock()
	pkgEvQueue = nil
	pkgEvMu.Unlock()
	evs := make([]pkgEv, pkgEvPerReport+5)
	for i := range evs {
		evs[i] = pkgEv{T: int64(i), A: "install", P: "p"}
	}
	pkgEvPush(evs)
	first := pkgEvPeek()
	if len(first) != pkgEvPerReport || first[0].T != 0 {
		t.Fatalf("первая порция: %d", len(first))
	}
	pkgEvDrop(len(first))
	if rest := pkgEvPeek(); len(rest) != 5 || rest[0].T != int64(pkgEvPerReport) {
		t.Fatalf("остаток: %+v", rest)
	}
	pkgEvDrop(100)
	if pkgEvPeek() != nil {
		t.Fatal("очередь пуста")
	}
}
