package main

import (
	"reflect"
	"testing"
)

func TestDockerCmdNames(t *testing.T) {
	cases := []struct {
		args []string
		want []string
	}{
		{[]string{"/usr/bin/docker", "run", "--rm", "--name", "edge-proxy", "-p", "8080:8080", "haproxy:3.0"}, []string{"edge-proxy"}},
		{[]string{"docker", "-H", "unix:///run/docker.sock", "container", "run", "--name=web", "nginx"}, []string{"web"}},
		{[]string{"/bin/sh", "-c", "'docker", "rm", "-f", "app;", "docker", "run", "--name", "app", "img'"}, []string{"app"}},
		{[]string{"docker", "start", "-a", "--detach-keys", "ctrl-x", "db"}, []string{"db"}},
		{[]string{"docker", "rm", "-f", "edge-proxy"}, nil},
		{[]string{"docker", "run", "--rm", "img"}, nil},
		{[]string{"dockerd", "--name", "x"}, nil},
	}
	for _, c := range cases {
		if got := dockerCmdNames(c.args); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%v: %v, want %v", c.args, got, c.want)
		}
	}
}

func TestCgroupUnit(t *testing.T) {
	for cg, want := range map[string]string{
		"0::/system.slice/edge-proxy.service\n":                                         "edge-proxy.service",
		"12:pids:/system.slice/app.service\n1:name=systemd:/system.slice/app.service\n": "app.service",
		"0::/user.slice/user-1000.slice/user@1000.service/app.slice/run.service\n":      "",
		"0::/system.slice/cron.service\n":                                               "",
		"0::/user.slice/user-1000.slice/session-3.scope\n":                              "",
	} {
		if got := cgroupUnit(cg); got != want {
			t.Errorf("%q: %q, want %q", cg, got, want)
		}
	}
}

// Контейнер с --rm и без restart policy, которого запускает юнит systemd, после перезагрузки
// поднимет systemd: агент отдает юнит и включен ли он. Три признака: ExecStart включенного юнита,
// процесс docker run в cgroup юнита, юнит с именем контейнера.
func TestDockerUnits(t *testing.T) {
	root := t.TempDir()
	etc, proc := root+"/etc", root+"/proc"
	writeFile(t, etc+"/multi-user.target.wants/edge-proxy.service", `[Service]
ExecStartPre=-/usr/bin/docker rm -f edge-proxy
ExecStart=/usr/bin/docker run --rm --name edge-proxy \
  -p 203.0.113.10:2181:2181 haproxy:3.0-alpine
Restart=always
`)
	// юнит со скриптом: контейнер видно только по процессу docker run в его cgroup
	writeFile(t, etc+"/multi-user.target.wants/worker.service", "[Service]\nExecStart=/opt/worker/run.sh\n")
	writeFile(t, proc+"/412/comm", "docker\n")
	writeFile(t, proc+"/412/cmdline", "docker\x00run\x00--rm\x00--name\x00worker-1\x00app/worker:2\x00")
	writeFile(t, proc+"/412/cgroup", "0::/system.slice/worker.service\n")
	// запущен из cron: cron не поднимет его после перезагрузки
	writeFile(t, proc+"/530/comm", "docker\n")
	writeFile(t, proc+"/530/cmdline", "docker\x00run\x00--name\x00nightly\x00app/job:1\x00")
	writeFile(t, proc+"/530/cgroup", "0::/system.slice/cron.service\n")
	// юнит с именем контейнера есть, но не включен
	writeFile(t, etc+"/metrics.service", "[Service]\nExecStart=/opt/metrics/start\n")

	cs := []dockerContainer{
		{Name: "edge-proxy", State: "running", Policy: "no", AutoRm: true},
		{Name: "worker-1", State: "running", Policy: "no", AutoRm: true},
		{Name: "nightly", State: "running", Policy: "no"},
		{Name: "metrics", State: "running", Policy: ""},
		{Name: "web", State: "running", Policy: "unless-stopped"},
	}
	dockerUnits(cs, proc, etc)
	got := map[string][2]any{}
	for _, c := range cs {
		got[c.Name] = [2]any{c.Unit, c.UnitOn}
	}
	want := map[string][2]any{
		"edge-proxy": {"edge-proxy.service", true},
		"worker-1":   {"worker.service", true},
		"nightly":    {"", false},
		"metrics":    {"metrics.service", false},
		"web":        {"", false},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
