package main

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPodMemCgroupfs(t *testing.T) {
	// k0s: cgroup v2, драйвер cgroupfs
	root := t.TempDir()
	pod := root + "/kubepods/burstable/pod3771a189-2044-4bbc-a55f-b7792cb5827b"
	writeFile(t, pod+"/memory.current", "51544064\n")
	writeFile(t, pod+"/memory.stat", "anon 6746112\ninactive_file 196608\n")
	// контейнер внутри пода не считается отдельно
	writeFile(t, pod+"/0123abcd/memory.current", "1000\n")
	// Guaranteed-под лежит прямо в kubepods
	writeFile(t, root+"/kubepods/pod11111111-2222-3333-4444-555555555555/memory.current", "4096\n")
	got := podMem(root)
	if got["3771a189-2044-4bbc-a55f-b7792cb5827b"] != 51544064-196608 || len(got) != 2 {
		t.Fatalf("cgroupfs: %v", got)
	}
}

func TestPodMemSystemdAndV1(t *testing.T) {
	root := t.TempDir()
	dir := root + "/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod0a1b2c3d_0000_1111_2222_333344445555.slice"
	writeFile(t, dir+"/memory.current", "2000\n")
	writeFile(t, dir+"/memory.stat", "inactive_file 500\n")
	if got := podMem(root); got["0a1b2c3d-0000-1111-2222-333344445555"] != 1500 {
		t.Fatalf("systemd: %v", got)
	}
	v1 := t.TempDir()
	d1 := v1 + "/memory/kubepods/burstable/podaaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
	writeFile(t, d1+"/memory.usage_in_bytes", "9000\n")
	writeFile(t, d1+"/memory.stat", "total_inactive_file 1000\n")
	if got := podMem(v1); got["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"] != 8000 {
		t.Fatalf("v1: %v", got)
	}
}

func TestPodCtrl(t *testing.T) {
	cases := []struct{ kind, name, hash, want string }{
		{"ReplicaSet", "access-hub-5965758d6c", "5965758d6c", "deployment/access-hub"},
		{"ReplicaSet", "bare-rs", "", ""},
		{"StatefulSet", "postgres", "", "statefulset/postgres"},
		{"DaemonSet", "node-exporter", "", "daemonset/node-exporter"},
		{"Job", "backup-29312", "", ""},
	}
	for _, c := range cases {
		if got := podCtrl(c.kind, c.name, c.hash); got != c.want {
			t.Errorf("%s %s: %q, want %q", c.kind, c.name, got, c.want)
		}
	}
	if podUID("pod3771a189-2044-4bbc-a55f-b7792cb5827b") == "" || podUID("kubepods-burstable") != "" || podUID("burstable") != "" {
		t.Error("podUID")
	}
}
