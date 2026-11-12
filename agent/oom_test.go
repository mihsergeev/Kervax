package main

import (
	"io"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestReadOOMVictimNonblockingFIFO(t *testing.T) {
	path := filepath.Join(t.TempDir(), "kmsg")
	if err := syscall.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	// Держим writer открытым: после записи будет EAGAIN, а не EOF.
	writer, err := syscall.Open(path, syscall.O_RDWR|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if writer >= 0 {
			syscall.Close(writer)
		}
	}()
	if _, err := syscall.Write(writer, []byte("3,1,123,-;Out of memory: Killed process 42 (worker)\n")); err != nil {
		t.Fatal(err)
	}
	done := make(chan string, 1)
	go func() { done <- readOOMVictimPath(path) }()
	select {
	case got := <-done:
		if got != "worker (pid 42)" {
			t.Fatalf("got %q", got)
		}
	case <-time.After(2 * time.Second):
		// Освобождаем возможное os.File.Read при регрессии перед завершением теста.
		syscall.Close(writer)
		writer = -1
		select {
		case <-done:
		case <-time.After(time.Second):
		}
		t.Fatal("reader waited for new messages after draining nonblocking FIFO")
	}
	if got := readOOMVictimPath(path); got != "" {
		t.Fatalf("empty nonblocking FIFO returned %q", got)
	}
}

func TestReadOOMVictimRecords(t *testing.T) {
	type record struct {
		text string
		err  error
	}
	for _, tc := range []struct {
		name    string
		records []record
		want    string
	}{
		{"last victim", []record{{text: "3,1,1,-;Killed process 1 (old)"}, {text: "6,2,2,-;unrelated"}, {text: "3,3,3,-;oom-kill:constraint=NONE,task=latest,pid=2,uid=0"}, {err: syscall.EAGAIN}}, "latest (pid 2)"},
		{"interrupt and overwritten ring", []record{{text: "Killed process 1 (old)"}, {err: syscall.EINTR}, {err: syscall.EPIPE}, {text: "Killed process 3 (new)"}, {err: syscall.EAGAIN}}, "new (pid 3)"},
		{"EOF preserves victim", []record{{text: "Killed process 4 (worker)"}, {err: io.EOF}}, "worker (pid 4)"},
		{"zero read preserves victim", []record{{text: "Killed process 4 (worker)"}, {}}, "worker (pid 4)"},
		{"unreadable", []record{{err: syscall.EACCES}}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			got := readOOMVictimRecords(func(buf []byte) (int, error) {
				if calls >= len(tc.records) {
					t.Fatal("read continued past end of input")
				}
				r := tc.records[calls]
				calls++
				return copy(buf, r.text), r.err
			})
			if got != tc.want || calls != len(tc.records) {
				t.Fatalf("got %q after %d reads, want %q after %d", got, calls, tc.want, len(tc.records))
			}
		})
	}
}

func TestReadOOMVictimBounded(t *testing.T) {
	for _, err := range []error{nil, syscall.EINTR, syscall.EPIPE} {
		calls := 0
		got := readOOMVictimRecords(func(buf []byte) (int, error) {
			calls++
			if calls == 1 {
				return copy(buf, "Killed process 5 (worker)"), nil
			}
			return copy(buf, "unrelated"), err
		})
		if got != "worker (pid 5)" || calls < 2 || calls > oomVictimMaxReads {
			t.Fatalf("err %v: got %q after %d reads", err, got, calls)
		}
	}
}

func TestReadOOMVictimMissingPath(t *testing.T) {
	if got := readOOMVictimPath(filepath.Join(t.TempDir(), "missing")); got != "" {
		t.Fatalf("missing path returned %q", got)
	}
}

func TestReadOOMVictimTimeBudget(t *testing.T) {
	calls := 0
	got := readOOMVictimRecords(func(buf []byte) (int, error) {
		calls++
		if calls > 1 {
			t.Fatal("read continued after time budget was exhausted")
		}
		time.Sleep(275 * time.Millisecond)
		return copy(buf, "Killed process 6 (worker)"), nil
	})
	if got != "worker (pid 6)" || calls != 1 {
		t.Fatalf("got %q after %d reads", got, calls)
	}
}
