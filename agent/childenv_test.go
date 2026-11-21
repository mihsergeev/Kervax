package main

import (
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// HOME и DOCKER_CONFIG дочерних программ - childHome, по одному разу, без /tmp и NOTIFY_SOCKET
func TestCleanEnvChildHome(t *testing.T) {
	t.Setenv("HOME", "/tmp")
	t.Setenv("DOCKER_CONFIG", "/tmp/.docker")
	t.Setenv("NOTIFY_SOCKET", "/run/systemd/notify")
	got := map[string][]string{}
	for _, kv := range cleanEnv() {
		k, v, _ := strings.Cut(kv, "=")
		got[k] = append(got[k], v)
	}
	for _, k := range []string{"HOME", "DOCKER_CONFIG"} {
		if len(got[k]) != 1 || got[k][0] != childHome {
			t.Errorf("%s=%v, ждали ровно %s", k, got[k], childHome)
		}
	}
	if len(got["NOTIFY_SOCKET"]) != 0 {
		t.Errorf("NOTIFY_SOCKET ушел детям: %v", got["NOTIFY_SOCKET"])
	}
}

// Создать childHome может только root: каталога нет или он root без записи для остальных,
// и так же все каталоги выше. Общедоступный /tmp сюда не годится.
func TestChildHomeOnlyRoot(t *testing.T) {
	// прямо в «/»: предки всегда проходимы, агент видит «нет такого», а не «нет доступа»
	if filepath.Dir(childHome) != "/" || childHome == "/tmp" || childHome == "/var" {
		t.Fatalf("childHome=%s", childHome)
	}
	if !rootOnlyPath(childHome, 0) {
		t.Fatalf("%s или каталог выше доступен на запись не только root", childHome)
	}
	// общедоступный каталог, симлинк (/proc/self), файл вместо каталога, чужой владелец, не абсолютный путь
	for _, p := range []string{"/tmp", "/tmp/.docker/cli-plugins", "/var/tmp/x", "/proc/self/x", "/etc/passwd",
		"/etc/passwd/x", "relative/path"} {
		if rootOnlyPath(p, 0) {
			t.Errorf("%s принят как каталог только root", p)
		}
	}
	// ошибка Lstat кроме «нет такого» - отказ, даже если каталоги выше в порядке
	if rootOnlyPath("/"+strings.Repeat("k", 300), 0) {
		t.Error("имя длиннее NAME_MAX принято")
	}
	if os.Getuid() != 0 {
		var st syscall.Stat_t
		if syscall.Lstat("/root", &st) == nil && st.Uid == 0 && st.Mode&0o077 == 0 && rootOnlyPath("/root/kervax-test", 0) {
			t.Error("/root/kervax-test (нет доступа) принят")
		}
	}
	if rootOnlyPath("/", 12345) {
		t.Error("«/» принят как каталог uid 12345")
	}
	// несуществующий хвост под каталогами root - годится: создать его может только root
	if !rootOnlyPath("/nonexistent-kervax-test/a/b", 0) {
		t.Error("несуществующий путь под «/» отвергнут")
	}
}

// docker CLI получает окружение агента: фальшивый docker печатает свои HOME и DOCKER_CONFIG
func TestDockerExecEnv(t *testing.T) {
	if !rootOnlyPath(childHome, 0) {
		t.Skip("на этой машине каталоги выше childHome не root - dockerExec docker не запустит")
	}
	t.Setenv("HOME", "/tmp")
	dir := t.TempDir()
	fake := filepath.Join(dir, "docker")
	if err := os.WriteFile(fake, []byte("#!/bin/sh\necho \"$HOME|$DOCKER_CONFIG|$*\"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	want := childHome + "|" + childHome + "|compose version --short"
	if got := dockerExec(fake, "compose", "version", "--short"); got != want {
		t.Fatalf("dockerExec: %q, ждали %q", got, want)
	}
	if got := cmdOut(3*time.Second, fake, "compose", "version", "--short"); got != want {
		t.Fatalf("cmdOut: %q, ждали %q", got, want)
	}
}

// Небезопасный childHome - docker CLI не запускается вовсе
func TestDockerExecRefusesUnsafeHome(t *testing.T) {
	dir := t.TempDir()
	old := childHome
	childHome = filepath.Join(dir, "home") // под общедоступным /tmp
	t.Cleanup(func() { childHome = old })
	fake := filepath.Join(dir, "docker")
	if err := os.WriteFile(fake, []byte("#!/bin/sh\ntouch '"+dir+"/ran'\necho ran\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := dockerExec(fake, "compose", "version", "--short"); got != "" {
		t.Fatalf("dockerExec: %q", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "ran")); err == nil {
		t.Fatal("docker CLI запущен с небезопасным HOME")
	}
	// тот же фальшивый docker с безопасным HOME запускается: тест проверяет именно проверку пути
	childHome = old
	if !rootOnlyPath(childHome, 0) {
		t.Skip("каталоги выше childHome на этой машине не root")
	}
	if got := dockerExec(fake, "compose", "version", "--short"); got != "ran" {
		t.Fatalf("dockerExec с безопасным HOME: %q", got)
	}
}

// Запрос не записался целиком (кончилось место, лимит размера) - недописанный .tmp не остается
// в спуле: иначе path-юнит helper'а крутится на нем до start-limit
func TestSpoolRemovesPartialTmp(t *testing.T) {
	dir := t.TempDir()
	var old syscall.Rlimit
	if err := syscall.Getrlimit(syscall.RLIMIT_FSIZE, &old); err != nil {
		t.Fatal(err)
	}
	signal.Ignore(syscall.SIGXFSZ) // иначе превышение лимита убивает процесс теста
	defer signal.Reset(syscall.SIGXFSZ)
	lim := old
	lim.Cur = 16
	if err := syscall.Setrlimit(syscall.RLIMIT_FSIZE, &lim); err != nil {
		t.Skip("не поставить RLIMIT_FSIZE:", err)
	}
	ok, out := spoolInFor(dir, dir, 7, []string{"action=provision", "repopass=" + strings.Repeat("x", 64)}, "test", time.Second)
	if err := syscall.Setrlimit(syscall.RLIMIT_FSIZE, &old); err != nil {
		t.Fatal(err)
	}
	if ok || !strings.Contains(out, "спул недоступен") {
		t.Fatalf("%v %q", ok, out)
	}
	if ents, _ := os.ReadDir(dir); len(ents) != 0 {
		t.Fatalf("в спуле остались файлы: %v", ents)
	}
}
