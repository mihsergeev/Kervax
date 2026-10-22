package main

import "testing"

func TestKindFromSystemd(t *testing.T) {
	cases := []struct {
		jobs  string
		ok    bool
		state string
		want  string
	}{
		{"1234 reboot.target start waiting\n1235 systemd-reboot.service start waiting\n", true, "stopping\n", "reboot"},
		{"77 poweroff.target start waiting\n", true, "stopping\n", "poweroff"},
		{"9 kexec.target start waiting\n", true, "stopping\n", "reboot"},
		{"5 shutdown.target start waiting\n", true, "stopping\n", "shutdown"},
		// шина уже закрылась, но состояние известно
		{"", false, "stopping\n", "shutdown"},
		// systemctl не ответил вовсе - система останавливается
		{"", false, "", "shutdown"},
		// остановили только агента
		{"", true, "running\n", "stop"},
		{"12 apt-daily.service start running\n", true, "degraded\n", "stop"},
	}
	for _, c := range cases {
		if got := kindFromSystemd(c.jobs, c.ok, c.state); got != c.want {
			t.Errorf("kindFromSystemd(%q, %v, %q) = %q, want %q", c.jobs, c.ok, c.state, got, c.want)
		}
	}
}
