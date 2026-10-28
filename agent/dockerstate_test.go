package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
)

// dockerRT отправляет запросы агента на http://docker/... в тестовый сервер.
type dockerRT struct{ base *url.URL }

func (r dockerRT) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.URL.Scheme, req.URL.Host = r.base.Scheme, r.base.Host
	return http.DefaultTransport.RoundTrip(req)
}

// У остановленного контейнера агент отдает код выхода и State.Error: по ним панель отличает
// остановку руками (ошибки нет) от контейнера, который докер не смог запустить.
func TestDockerInspectStopped(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/containers/manual/json":
			w.Write([]byte(`{"RestartCount":0,"State":{"Error":"","ExitCode":0},"HostConfig":{"RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0}}}`))
		case "/containers/failed/json":
			w.Write([]byte(`{"RestartCount":2,"State":{"Error":"  Bind for 0.0.0.0:80 failed: port is already allocated\n","ExitCode":128,"OOMKilled":true},"HostConfig":{"RestartPolicy":{"Name":"on-failure","MaximumRetryCount":5}}}`))
		case "/containers/up/json":
			w.Write([]byte(`{"RestartCount":1,"State":{"Error":"","ExitCode":0},"HostConfig":{"RestartPolicy":{"Name":"always"}}}`))
		case "/containers/rm/json":
			w.Write([]byte(`{"State":{},"HostConfig":{"RestartPolicy":{"Name":"no"},"AutoRemove":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	cl := &http.Client{Transport: dockerRT{base}}

	m := dockerContainer{Name: "manual", State: "exited"}
	dockerInspect(cl, "manual", &m)
	if m.Exit == nil || *m.Exit != 0 || m.Err != "" || m.Policy != "unless-stopped" || m.MaxRetry != 0 {
		t.Fatalf("manual: %+v", m)
	}
	f := dockerContainer{Name: "failed", State: "exited"}
	dockerInspect(cl, "failed", &f)
	if f.Exit == nil || *f.Exit != 128 || f.MaxRetry != 5 || !f.OOM ||
		f.Err != "Bind for 0.0.0.0:80 failed: port is already allocated" {
		t.Fatalf("failed: %+v", f)
	}
	u := dockerContainer{Name: "up", State: "running"}
	dockerInspect(cl, "up", &u)
	if u.Exit != nil || u.Err != "" || u.Restarts != 1 {
		t.Fatalf("running container must not carry exit/err: %+v", u)
	}
	rm := dockerContainer{Name: "rm", State: "running"}
	dockerInspect(cl, "rm", &rm)
	if !rm.AutoRm || u.AutoRm {
		t.Fatalf("docker run --rm must carry auto_rm: %+v %+v", rm, u)
	}
	// в отчете код 0 у остановленного виден (указатель), у работающего этих полей нет
	b, _ := json.Marshal(m)
	if !strings.Contains(string(b), `"exit":0`) || strings.Contains(string(b), `"err"`) {
		t.Fatalf("manual json: %s", b)
	}
	b, _ = json.Marshal(u)
	if strings.Contains(string(b), `"exit"`) || strings.Contains(string(b), `"max_retry"`) {
		t.Fatalf("running json: %s", b)
	}
}

// Ключи от хоста: privileged, опасные --cap-add (с префиксом CAP_ и без), --pid=host и какие
// bind-монтирования только на чтение. Безобидные capabilities (NET_ADMIN) не шлем.
func TestDockerInspectHostAccess(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/containers/vpn/json":
			w.Write([]byte(`{"HostConfig":{"Privileged":true,"CapAdd":["NET_ADMIN","CAP_SYS_MODULE","sys_module"],"PidMode":"host"},` +
				`"Mounts":[{"Type":"bind","Source":"/","RW":false},{"Type":"bind","Source":"/var/run/docker.sock","RW":true},` +
				`{"Type":"volume","Source":"/var/lib/docker/volumes/x/_data","RW":false}]}`))
		case "/containers/app/json":
			w.Write([]byte(`{"HostConfig":{"CapAdd":["NET_ADMIN"],"PidMode":""},"Mounts":[{"Type":"bind","Source":"/srv/app","RW":true}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	cl := &http.Client{Transport: dockerRT{base}}

	v := dockerContainer{Name: "vpn", State: "running"}
	dockerInspect(cl, "vpn", &v)
	if !v.Priv || !v.PidHost || strings.Join(v.Caps, ",") != "SYS_MODULE" || strings.Join(v.RO, ",") != "/" ||
		strings.Join(v.Binds, ",") != "/,/var/run/docker.sock" {
		t.Fatalf("vpn: %+v", v)
	}
	a := dockerContainer{Name: "app", State: "running"}
	dockerInspect(cl, "app", &a)
	b, _ := json.Marshal(a)
	for _, k := range []string{`"priv"`, `"caps"`, `"pid_host"`, `"ro"`} {
		if strings.Contains(string(b), k) {
			t.Fatalf("app json carries %s: %s", k, b)
		}
	}
}

// Права главного процесса: cap_add DAC_READ_SEARCH у контейнера, который на старте делает
// setpriv --reuid и работает без прав, и у того, кто остался root или держит cap в permitted.
func TestParseProcPrivs(t *testing.T) {
	st := func(uid, eff, prm, nnp string) string {
		return strings.Join([]string{"Name:\tapp", "Uid:\t" + uid, "Gid:\t10001\t10001\t10001\t10001",
			"CapInh:\t0000000000000000", "CapPrm:\t" + prm, "CapEff:\t" + eff, "CapBnd:\t00000000000000c5",
			"CapAmb:\t0000000000000000", "NoNewPrivs:\t" + nnp, "Seccomp:\t2", ""}, "\n")
	}
	dropped := parseProcPrivs(st("10001\t10001\t10001\t10001", "0000000000000000", "0000000000000000", "1"))
	if dropped == nil || dropped.Uid != 10001 || len(dropped.Caps) != 0 || !dropped.NNP {
		t.Fatalf("dropped: %+v", dropped)
	}
	// saved uid 0: процесс вернет себе root, значит root
	saved := parseProcPrivs(st("10001\t10001\t0\t10001", "0000000000000000", "0000000000000000", "1"))
	if saved == nil || saved.Uid != 0 {
		t.Fatalf("saved: %+v", saved)
	}
	// DAC_READ_SEARCH (бит 2) только в permitted - поднимет в effective сам; CHOWN (бит 0) не опасен
	prm := parseProcPrivs(st("10001\t10001\t10001\t10001", "0000000000000001", "0000000000000005", "0"))
	if prm == nil || strings.Join(prm.Caps, ",") != "DAC_READ_SEARCH" || prm.NNP {
		t.Fatalf("permitted: %+v", prm)
	}
	all := parseProcPrivs(st("0\t0\t0\t0", "000001ffffffffff", "000001ffffffffff", "0"))
	if all == nil || strings.Join(all.Caps, ",") != "DAC_READ_SEARCH,SYS_MODULE,SYS_RAWIO,SYS_PTRACE,SYS_ADMIN,BPF" {
		t.Fatalf("root: %+v", all)
	}
	for _, bad := range []string{"", "Name:\tapp\n", st("x\t1\t1\t1", "0", "0", "1"), st("1\t1\t1\t1", "zz", "0", "1")} {
		if p := parseProcPrivs(bad); p != nil {
			t.Fatalf("%q: %+v", bad, p)
		}
	}
	b, _ := json.Marshal(dropped)
	if string(b) != `{"uid":10001,"nnp":true}` {
		t.Fatalf("json: %s", b)
	}
}

// proc шлем только работающему контейнеру с опасными caps: pid берется из State.Pid inspect
func TestDockerInspectProcPrivs(t *testing.T) {
	pid := os.Getpid()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/containers/shocker/json":
			fmt.Fprintf(w, `{"State":{"Pid":%d},"HostConfig":{"CapAdd":["DAC_READ_SEARCH","CHOWN"]}}`, pid)
		case "/containers/net/json":
			fmt.Fprintf(w, `{"State":{"Pid":%d},"HostConfig":{"CapAdd":["NET_ADMIN"]}}`, pid)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	cl := &http.Client{Transport: dockerRT{base}}

	sh := dockerContainer{Name: "shocker", State: "running"}
	dockerInspect(cl, "shocker", &sh)
	if sh.Proc == nil || sh.Proc.Uid > os.Getuid() {
		t.Fatalf("shocker: %+v", sh.Proc)
	}
	stopped := dockerContainer{Name: "shocker", State: "exited"}
	dockerInspect(cl, "shocker", &stopped)
	n := dockerContainer{Name: "net", State: "running"}
	dockerInspect(cl, "net", &n)
	if stopped.Proc != nil || n.Proc != nil {
		t.Fatalf("proc without danger caps: %+v %+v", stopped.Proc, n.Proc)
	}
}
