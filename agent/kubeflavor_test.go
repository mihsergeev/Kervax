package main

import (
	"os"
	"path/filepath"
	"testing"
)

func fakeHostProc(t *testing.T, procs map[string][2]string) {
	t.Helper()
	dir := t.TempDir()
	for pid, p := range procs {
		d := filepath.Join(dir, pid)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		os.WriteFile(filepath.Join(d, "comm"), []byte(p[0]+"\n"), 0o644)
		os.WriteFile(filepath.Join(d, "cgroup"), []byte(p[1]), 0o644)
	}
	old := procRoot
	procRoot = dir
	t.Cleanup(func() { procRoot = old })
}

// k3s из образа rancher/k3s в контейнере Docker - не кластер хоста (одна нода, 27.10.2026)
func TestProcRunningSkipsDockerContainer(t *testing.T) {
	fakeHostProc(t, map[string][2]string{
		"100": {"k3s", "0::/system.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope\n"},
		"101": {"kubelet", "12:memory:/docker/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"},
	})
	if procRunning("k3s") {
		t.Fatal("k3s в контейнере Docker принят за кластер хоста")
	}
	if procRunning("kubelet") {
		t.Fatal("kubelet в контейнере Docker (cgroup v1) принят за кластер хоста")
	}
}

func TestProcRunningHostAndPods(t *testing.T) {
	fakeHostProc(t, map[string][2]string{
		"1":  {"k3s-server", "0::/system.slice/k3s.service\n"},
		"2":  {"kube-apiserver", "0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod1f2e3d4c_5b6a_7980_1a2b_3c4d5e6f7a8b.slice/docker-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.scope\n"},
		"3":  {"kubelet", "0::/system.slice/kubelet.service\n"},
		"4":  {"k0s", "0::/\n"},
		"55": {"nginx", "0::/system.slice/docker-aa.scope\n"},
	})
	for _, n := range []string{"k3s-server", "kube-apiserver", "kubelet", "k0s"} {
		if !procRunning(n) {
			t.Fatalf("%s на хосте не найден", n)
		}
	}
	if procRunning("microk8s") {
		t.Fatal("нет такого процесса")
	}
}

func TestInDockerCt(t *testing.T) {
	cases := map[string]bool{
		"0::/system.slice/docker-abc.scope":                                              true,
		"0::/system.slice/docker-abc.scope/kubelet.slice/kubelet-kubepods.slice/x.scope": true, // kind
		"0::/kubepods.slice/kubepods-pod1.slice/docker-abc.scope":                        false,
		"0::/system.slice/kubelet.service":                                               false,
		"0::/":                                                                           false,
		"":                                                                               false,
	}
	for cg, want := range cases {
		if got := inDockerCt(cg); got != want {
			t.Errorf("%q: %v, ждали %v", cg, got, want)
		}
	}
}
