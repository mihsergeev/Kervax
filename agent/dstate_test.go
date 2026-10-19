package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
)

// procStatLine - строка /proc/<pid>/stat: pid, comm, state, ... starttime (поле 22), vsize, rss
func procStatLine(pid int, comm, state string, start uint64) string {
	return fmt.Sprintf("%d (%s) %s 1 1 1 0 -1 4194560 10 0 0 0 5 3 0 0 20 0 1 0 %d 1000 25 18446744073709551615",
		pid, comm, state, start)
}

func procStatus(vol, nonvol uint64) string {
	return fmt.Sprintf("Name:\tx\nState:\tD (disk sleep)\nvoluntary_ctxt_switches:\t%d\nnonvoluntary_ctxt_switches:\t%d\n",
		vol, nonvol)
}

// fakeProc - снимок /proc: тексты stat, status и wchan по pid
type fakeProc struct {
	stat, status, wchan map[int]string
}

func (f fakeProc) procs(t *testing.T) map[int]procSample {
	m := map[int]procSample{}
	for pid, s := range f.stat {
		p, ok := parseProcStat(s, 4096)
		if !ok {
			t.Fatalf("stat %d не разобрался: %q", pid, s)
		}
		m[pid] = p
	}
	return m
}

func (f fakeProc) read(pid int, name string) string {
	switch name {
	case "status":
		return f.status[pid]
	case "wchan":
		return f.wchan[pid]
	}
	return ""
}

func TestParseProcStat(t *testing.T) {
	p, ok := parseProcStat(procStatLine(4242, "postgres", "D", 5000), 4096)
	if !ok || p.comm != "postgres" || p.state != "D" || p.start != 5000 || p.ticks != 8 || p.rss != 25*4096 {
		t.Fatalf("stat: %+v %v", p, ok)
	}
	// comm со скобками и пробелом: берем по последней ')'
	p, ok = parseProcStat(procStatLine(7, "a) (b", "S", 9), 4096)
	if !ok || p.comm != "a) (b" || p.start != 9 {
		t.Fatalf("comm со скобками: %+v", p)
	}
	if _, ok := parseProcStat("7 (x) S 1 2", 4096); ok {
		t.Fatal("короткая строка")
	}
	if sw, ok := ctxtSwitches(procStatus(150, 7)); !ok || sw != 157 {
		t.Fatalf("switches: %d %v", sw, ok)
	}
	if _, ok := ctxtSwitches("Name:\tx\nvoluntary_ctxt_switches:\t1\n"); ok {
		t.Fatal("нет nonvoluntary")
	}
}

func TestDstateStuck(t *testing.T) {
	t0 := time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC)
	f := fakeProc{
		stat: map[int]string{
			100: procStatLine(100, "postgres", "D", 5000), // висит: переключений не прибавляется
			200: procStatLine(200, "postgres", "D", 5100), // в D каждый отчет, но просыпается
			300: procStatLine(300, "gzip", "D", 7000),     // pid переиспользуют
			400: procStatLine(400, "bash", "S", 7100),
		},
		status: map[int]string{100: procStatus(150, 7), 200: procStatus(10, 1), 300: procStatus(40, 2), 400: procStatus(1, 1)},
		wchan:  map[int]string{100: "folio_wait_bit_common\n", 300: "0"},
	}
	var tr dstateTracker
	step := func(sec int) []dstateStuck {
		return tr.stuck(f.procs(t), f.read, t0.Add(time.Duration(sec)*time.Second))
	}
	for i, sec := range []int{0, 15, 60, 105} {
		if got := step(sec); len(got) != 0 {
			t.Fatalf("шаг %d: рано считать зависшим: %+v", i, got)
		}
		f.status[200] = procStatus(uint64(11+i), 1)
	}
	// новый процесс с тем же pid: другой starttime, а переключений столько же
	f.stat[300] = procStatLine(300, "gzip", "D", 9000)
	got := step(130)
	if len(got) != 1 || got[0] != (dstateStuck{PID: 100, Comm: "postgres", Wchan: "folio_wait_bit_common", Sec: 130}) {
		t.Fatalf("зависший: %+v", got)
	}
	f.status[200] = procStatus(20, 1)
	got = step(250)
	if len(got) != 2 || got[0].PID != 100 || got[0].Sec != 250 || got[1].PID != 300 || got[1].Sec != 120 || got[1].Wchan != "" {
		t.Fatalf("второй зависший без wchan (0 у чужого процесса): %+v", got)
	}
	// проснулся хоть раз - отсчет заново
	f.status[100] = procStatus(151, 7)
	if got = step(265); len(got) != 1 || got[0].PID != 300 {
		t.Fatalf("проснувшийся не зависший: %+v", got)
	}
	// вышел из D - забыт, вернулся с теми же переключениями - тоже заново
	f.stat[300] = procStatLine(300, "gzip", "S", 9000)
	step(280)
	f.stat[300] = procStatLine(300, "gzip", "D", 9000)
	if got = step(295); len(got) != 0 {
		t.Fatalf("после выхода из D: %+v", got)
	}
	if len(tr.seen) != 3 {
		t.Fatalf("помним только процессы в D: %+v", tr.seen)
	}
}

func TestHungGrowth(t *testing.T) {
	t0 := time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC)
	var tr dstateTracker
	at := func(min int) time.Time { return t0.Add(time.Duration(min) * time.Minute) }
	if g := tr.hungGrowth(3, at(0)); g != 0 {
		t.Fatalf("первый замер - точка отсчета: %d", g)
	}
	if g := tr.hungGrowth(5, at(4)); g != 2 {
		t.Fatalf("рост: %d", g)
	}
	tr.hungGrowth(5, at(8))
	// база - замер не моложе 10 минут (минута 4), рост с тех пор 0
	if g := tr.hungGrowth(5, at(14)); g != 0 {
		t.Fatalf("окно сдвинулось: %d %+v", g, tr.hung)
	}
	if g := tr.hungGrowth(0, at(15)); g != 0 {
		t.Fatalf("счетчик сбросили: %d", g)
	}
	if len(tr.hung) != 1 {
		t.Fatalf("после сброса история заново: %+v", tr.hung)
	}
}

func TestDstateJSON(t *testing.T) {
	d := dstate(map[int]procSample{1: {comm: "postgres", state: "D"}})
	b, _ := json.Marshal(d)
	// пустой список, а не null и не пропуск: панель по нему видит, что агент считает зависших
	if !strings.Contains(string(b), `"stuck":[]`) || strings.Contains(string(b), "hung") {
		t.Fatalf("json: %s", b)
	}
	n := uint64(4)
	d.Stuck = []dstateStuck{{PID: 9, Comm: "jbd2", Sec: 130}}
	d.Hung, d.HungNew = &n, 1
	b, _ = json.Marshal(d)
	if !strings.Contains(string(b), `"stuck":[{"pid":9,"comm":"jbd2","sec":130}],"hung":4,"hung_new":1`) {
		t.Fatalf("json: %s", b)
	}
}
