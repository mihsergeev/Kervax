package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
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
