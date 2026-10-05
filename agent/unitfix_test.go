package main

import "testing"

func TestUnitFixLines(t *testing.T) {
	ok := []struct{ mode, name string }{
		{"restart", "certbot.service"},
		{"reset", "mdmonitor-oneshot.service"},
		{"restart", `mnt-my\x20disk.mount`},
		{"restart", "getty@tty1.service"},
	}
	for _, c := range ok {
		lines, good := unitFixLines(backupCommand{Mode: c.mode, Name: c.name})
		if !good || len(lines) != 2 || lines[0] != "op="+c.mode || lines[1] != "unit="+c.name {
			t.Errorf("%s %s: %v %v", c.mode, c.name, lines, good)
		}
	}
	bad := []struct{ mode, name string }{
		{"stop", "certbot.service"},
		{"restart", "certbot"},
		{"restart", "-x.service"},
		{"restart", "a b.service"},
		{"restart", "x.service; reboot"},
		{"restart", "session-1.scope"},
		{"restart", "../x.service"},
	}
	for _, c := range bad {
		if _, good := unitFixLines(backupCommand{Mode: c.mode, Name: c.name}); good {
			t.Errorf("%s %q must be refused", c.mode, c.name)
		}
	}
}
