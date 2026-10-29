package main

import (
	"strings"
	"testing"
)

// Топ памяти с владельцами: по ним панель называет контейнер, съевший память перед пропажей агента
func TestOwnTopMem(t *testing.T) {
	id := strings.Repeat("cd", 32)
	uid := "56279ecf-da54-4be1-ab7e-f3774036245e"
	cgs := map[int]string{
		10: "0::/system.slice/docker-" + id + ".scope\n",
		11: "0::/system.slice/docker-" + strings.Repeat("ef", 32) + ".scope\n",
		12: "0::/kubepods/besteffort/pod" + uid + "/c1\n",
		13: "0::/system.slice/postgresql.service\n",
		14: "0::/user.slice/user-0.slice/session-1.scope\n",
	}
	old := procCgroup
	procCgroup = func(pid int) string { return cgs[pid] }
	defer func() { procCgroup = old }()

	top := []procStat{{Pid: 10, Comm: "java"}, {Pid: 11, Comm: "java"}, {Pid: 12, Comm: "node"},
		{Pid: 13, Comm: "postgres"}, {Pid: 14, Comm: "bash"}}
	ownTopMem(top, map[string]string{id: "runner-build-1"}, map[string]string{})
	want := [][2]string{{"container", "runner-build-1"}, {"container", "efefefefefef"},
		{"pod", "pod 56279ecf"}, {"unit", "postgresql.service"}, {"", ""}}
	for i, w := range want {
		if top[i].Kind != w[0] || top[i].Owner != w[1] {
			t.Errorf("%d: %q %q, want %q %q", top[i].Pid, top[i].Kind, top[i].Owner, w[0], w[1])
		}
	}
}
