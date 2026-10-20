package main

import (
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// proxy= из конфига: принимаем только http/https/socks5 с хостом, и ни одна ошибка не
// цитирует строку целиком - в ней пароль, а ошибка уходит в журнал.
func TestParseProxy(t *testing.T) {
	for _, ok := range []string{
		"http://user:secret@proxy.example:3128",
		"http://proxy.example:3128",
		"https://user:secret@proxy.example:8443/",
		"socks5://user:secret@proxy.example:1080",
	} {
		if _, err := parseProxy(ok); err != nil {
			t.Errorf("%s: %v", ok, err)
		}
	}
	for _, bad := range []string{
		"proxy.example:3128",
		"ftp://user:secret@proxy.example:21",
		"http://user:secret@:3128",
		"http://user:secret@proxy.example:3128/path",
		"http://user:secret@proxy.example:3128/?q=1",
		"http://user:secret@proxy.example:3128\x7f",
	} {
		_, err := parseProxy(bad)
		if err == nil {
			t.Errorf("%q принят", bad)
			continue
		}
		if strings.Contains(err.Error(), "secret") {
			t.Errorf("пароль в тексте ошибки: %v", err)
		}
	}
}

// В лог и в отчет попадает только хост:порт.
func TestProxyLabelHasNoCredentials(t *testing.T) {
	defer func() { panelProxyURL = nil }()
	panelProxyURL, _ = parseProxy("http://user:secret@proxy.example:3128")
	if got := proxyLabel("https://panel.example"); got != "proxy.example:3128" {
		t.Fatalf("proxyLabel = %q", got)
	}
	panelProxyURL = nil
	t.Setenv("HTTPS_PROXY", "")
	t.Setenv("https_proxy", "")
	if got := proxyLabel("https://panel.example"); got != "" {
		t.Fatalf("без прокси proxyLabel = %q", got)
	}
}

// Отчет и прочие запросы в панель идут через прокси из конфига вместе с логином:
// прокси здесь - httptest-сервер, который отвечает сам и запоминает, что пришло.
func TestPanelRequestsGoThroughProxy(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seen = append(seen, r.URL.String()+" "+r.Header.Get("Proxy-Authorization"))
		mu.Unlock()
		io.WriteString(w, `{}`)
	}))
	defer proxy.Close()
	pu, _ := url.Parse(proxy.URL)
	pu.User = url.UserPassword("user", "secret")
	panelProxyURL = pu
	defer func() { panelProxyURL = nil }()

	if _, err := httpGetLimited("http://panel.example/api/agent/manifest", 1<<10); err != nil {
		t.Fatal(err)
	}
	resp, err := panelClient(5 * time.Second).Get("http://panel.example/api/agent/commands")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	auth := "Basic " + base64.StdEncoding.EncodeToString([]byte("user:secret"))
	mu.Lock()
	defer mu.Unlock()
	want := []string{
		"http://panel.example/api/agent/manifest " + auth,
		"http://panel.example/api/agent/commands " + auth,
	}
	if strings.Join(seen, "\n") != strings.Join(want, "\n") {
		t.Fatalf("через прокси прошло:\n%s", strings.Join(seen, "\n"))
	}
}
