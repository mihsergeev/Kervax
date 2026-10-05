package main

// Память контейнера по cgroup: использование без неактивного файлового кэша, как docker
// stats. Три раскладки cgroup, и чужой путь в id не должен увести чтение за пределы корня.

import (
	"os"
	"path/filepath"
	"testing"
)

func writeCg(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for n, v := range files {
		if err := os.WriteFile(filepath.Join(dir, n), []byte(v), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestContainerMem(t *testing.T) {
	root := t.TempDir()
	writeCg(t, filepath.Join(root, "system.slice", "docker-aaa.scope"), map[string]string{
		"memory.current": "1000\n", "memory.stat": "anon 700\ninactive_file 200\nactive_file 100\n"})
	writeCg(t, filepath.Join(root, "docker", "bbb"), map[string]string{
		"memory.current": "500\n", "memory.stat": "inactive_file 50\n"})
	writeCg(t, filepath.Join(root, "memory", "docker", "ccc"), map[string]string{
		"memory.usage_in_bytes": "900\n", "memory.stat": "total_inactive_file 300\n"})
	cases := map[string]uint64{"aaa": 800, "bbb": 450, "ccc": 600, "zzz": 0, "../aaa": 0, "": 0}
	for id, want := range cases {
		if got := containerMem(root, id); got != want {
			t.Fatalf("%q: %d, ждали %d", id, got, want)
		}
	}
}
