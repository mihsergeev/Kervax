package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func routeOf(t *testing.T, routes []proxyRoute, host, path string) proxyRoute {
	t.Helper()
	r, ok := matchProxyRoute(routes, host, path)
	if !ok {
		t.Fatalf("no route for %s%s in %+v", host, path, routes)
	}
	return r
}

// caddy-docker-proxy: адреса сайта в метке caddy, бэкенд - {{upstreams}} в сети caddy, литеральный
// адрес - сервис того же compose-проекта, handle_path с путем
func TestCaddyLabelRoutes(t *testing.T) {
	cs := []proxyCtr{
		{name: "caddy-1", image: "lucaslorentz/caddy-docker-proxy:2.13.1", nets: map[string]string{"caddy": "172.18.0.2"}},
		{name: "panel-frontend-1", project: "panel", service: "frontend",
			nets: map[string]string{"caddy": "172.18.0.3", "panel_internal": "172.20.0.4"},
			labels: map[string]string{
				"caddy":                             "panel.example.com",
				"caddy.@denied.not.remote_ip":       "203.0.113.5 172.18.0.1",
				"caddy.abort":                       "@denied",
				"caddy.reverse_proxy":               "{{upstreams 80}}",
				"caddy_1":                           "https://api.example.com, api2.example.com:443",
				"caddy_1.reverse_proxy":             "backend:8000",
				"caddy_1.handle_path":               "/admin/*",
				"caddy_1.handle_path.reverse_proxy": "{{upstreams https 8443}}",
				"caddy_2":                           "skip.example.com",
				"caddy_2.reverse_proxy":             "@api backend:8000",
			}},
		{name: "other-backend-1", project: "other", service: "backend", nets: map[string]string{"x": "172.21.0.9"}},
		{name: "panel-backend-1", project: "panel", service: "backend", nets: map[string]string{"panel_internal": "172.20.0.3"}},
	}
	routes := caddyLabelRoutes(cs)
	r := routeOf(t, routes, "panel.example.com", "/")
	if r.addr != "172.18.0.3:80" || r.scheme != "http" || r.name != "container panel-frontend-1:80" {
		t.Fatalf("panel: %+v", r)
	}
	r = routeOf(t, routes, "API2.example.com", "/v1")
	if r.addr != "172.20.0.3:8000" || r.name != "container panel-backend-1:8000" {
		t.Fatalf("api: %+v", r)
	}
	r = routeOf(t, routes, "api.example.com", "/admin/users")
	if r.addr != "172.18.0.3:8443" || r.scheme != "https" {
		t.Fatalf("admin: %+v", r)
	}
	if _, ok := matchProxyRoute(routes, "skip.example.com", "/"); ok {
		t.Fatal("route behind a named matcher taken")
	}
}

func TestCaddyfileRoutes(t *testing.T) {
	text := `{
	admin off
}
(common) {
	encode gzip
}
# comment { not a block
app.example.com, www.app.example.com {
	import common
	@denied not remote_ip 203.0.113.0/24
	abort @denied
	handle /api/* {
		reverse_proxy 127.0.0.1:9000
	}
	handle @static {
		file_server
	}
	reverse_proxy localhost:8080 {
		header_up Host {host}
	}
}
http://plain.example.com {
	reverse_proxy {
		to http://10.0.0.5:3000
	}
}
`
	routes := caddyfileRoutes(text, nil, true)
	if r := routeOf(t, routes, "www.app.example.com", "/"); r.addr != "localhost:8080" || r.name != "addr localhost:8080" {
		t.Fatalf("app: %+v", r)
	}
	if r := routeOf(t, routes, "app.example.com", "/api/x"); r.addr != "127.0.0.1:9000" {
		t.Fatalf("api: %+v", r)
	}
	if r := routeOf(t, routes, "plain.example.com", "/"); r.addr != "10.0.0.5:3000" {
		t.Fatalf("plain: %+v", r)
	}
	// тот же Caddyfile внутри контейнера: localhost там - сам caddy, до него с хоста не дойти
	if r, ok := matchProxyRoute(caddyfileRoutes(text, nil, false), "app.example.com", "/"); ok && strings.HasPrefix(r.addr, "localhost") {
		t.Fatalf("container localhost taken: %+v", r)
	}
}

func TestCaddyJSONRoutes(t *testing.T) {
	raw := `[{"match":[{"host":["Site.example.com"]}],"handle":[{"handler":"subroute","routes":[
		{"match":[{"not":[{"remote_ip":{"ranges":["203.0.113.5"]}}]}],"handle":[{"handler":"static_response","abort":true}]},
		{"match":[{"path":["/api/*"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"127.0.0.1:9443"}],"transport":{"protocol":"http","tls":{}}}]},
		{"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"172.18.0.3:80"}]}]}]}]}]`
	var routes []caddyJRoute
	if err := json.Unmarshal([]byte(raw), &routes); err != nil {
		t.Fatal(err)
	}
	got := caddyJSONRoutes(routes, nil, "", nil)
	if r := routeOf(t, got, "site.example.com", "/"); r.addr != "172.18.0.3:80" || r.scheme != "http" {
		t.Fatalf("main: %+v", r)
	}
	if r := routeOf(t, got, "site.example.com", "/api/v1"); r.addr != "127.0.0.1:9443" || r.scheme != "https" {
		t.Fatalf("api: %+v", r)
	}
}

func TestNginxRoutes(t *testing.T) {
	dir := t.TempDir()
	sites := filepath.Join(dir, "sites")
	if err := os.Mkdir(sites, 0o755); err != nil {
		t.Fatal(err)
	}
	main := "user www-data;\nevents { worker_connections 768; }\nhttp {\n  upstream app_up { server 127.0.0.1:8081 max_fails=3; }\n  include " +
		filepath.ToSlash(sites) + "/*.conf;\n}\nstream { server { listen 5000; proxy_pass 10.0.0.9:5000; } }\n"
	site := `server {
  listen 443 ssl;
  server_name shop.example.com .example.org;
  allow 203.0.113.5; deny all;
  location / { proxy_pass http://app_up; }
  location ^~ /static/ { root /srv; }
  location /api/ { proxy_pass https://10.0.0.7:8443/v2/; }
  location ~ \.php$ { proxy_pass http://127.0.0.1:9000; }
  location /var { proxy_pass http://$backend; }
}
server { listen 80 default_server; server_name _; location / { proxy_pass http://127.0.0.1:1; } }
`
	if err := os.WriteFile(filepath.Join(dir, "nginx.conf"), []byte(main), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sites, "shop.conf"), []byte(site), 0o644); err != nil {
		t.Fatal(err)
	}
	routes := nginxRoutes(filepath.Join(dir, "nginx.conf"), nil)
	if r := routeOf(t, routes, "shop.example.com", "/cart"); r.addr != "127.0.0.1:8081" || r.proxy != "nginx" {
		t.Fatalf("main: %+v", r)
	}
	if r := routeOf(t, routes, "a.example.org", "/api/x"); r.addr != "10.0.0.7:8443" || r.scheme != "https" {
		t.Fatalf("api: %+v", r)
	}
	if r := routeOf(t, routes, "example.org", "/var/x"); r.addr != "127.0.0.1:8081" {
		t.Fatalf("variable proxy_pass taken: %+v", r)
	}
	for _, r := range routes {
		if r.addr == "10.0.0.9:5000" || r.addr == "127.0.0.1:1" {
			t.Fatalf("stream or default server taken: %+v", r)
		}
	}
}

func TestMatchProxyRoute(t *testing.T) {
	routes := []proxyRoute{
		{hosts: []string{"*.example.com"}, addr: "wild"},
		{hosts: []string{"a.example.com"}, addr: "exact"},
		{hosts: []string{"a.example.com"}, path: "/api/", addr: "api"},
	}
	for host, want := range map[string]string{"a.example.com": "exact", "b.example.com": "wild"} {
		if r := routeOf(t, routes, host, "/"); r.addr != want {
			t.Fatalf("%s: %+v", host, r)
		}
	}
	if r := routeOf(t, routes, "a.example.com", "/api/v1"); r.addr != "api" {
		t.Fatalf("path: %+v", r)
	}
	if _, ok := matchProxyRoute(routes, "x.b.example.com", "/"); ok {
		t.Fatal("mask matched two labels")
	}
}

// selfCert - самоподписанный сертификат на имя
func selfCert(t *testing.T, name string) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: name},
		DNSNames: []string{name}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour)}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// tlsAt - https-сервер на адресе ip:port с сертификатом на name
func tlsAt(t *testing.T, addr, name string, h http.Handler) *httptest.Server {
	t.Helper()
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		t.Skipf("listen %s: %v", addr, err)
	}
	srv := httptest.NewUnstartedServer(h)
	srv.Listener.Close()
	srv.Listener = ln
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{selfCert(t, name)}}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv
}

// свободный порт, одинаковый на двух адресах лупбека
func freePort(t *testing.T) string {
	t.Helper()
	for i := 0; i < 20; i++ {
		ln, err := net.Listen("tcp", "127.0.0.2:0")
		if err != nil {
			t.Skipf("no 127.0.0.2: %v", err)
		}
		port := strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)
		ln.Close()
		ok := true
		for _, ip := range []string{"127.0.0.1", "127.0.0.3", "127.0.0.4"} {
			if c, err := net.DialTimeout("tcp", ip+":"+port, 200*time.Millisecond); err == nil {
				c.Close()
				ok = false
			}
		}
		if ok {
			return port
		}
	}
	t.Skip("no free port")
	return ""
}

func withProbePort(t *testing.T, port string) {
	t.Helper()
	localProbePorts[port] = true
	oldOwn, oldHost, oldLoad := ownIPs, hostIPs, proxyRoutesLoad
	t.Cleanup(func() {
		delete(localProbePorts, port)
		ownIPs, hostIPs, proxyRoutesLoad = oldOwn, oldHost, oldLoad
		proxyRoutesMu.Lock()
		proxyRoutesCache = nil
		proxyRoutesMu.Unlock()
	})
}

// localhost закрыт, прокси слушает только второй адрес сервера, а на первом тот же порт держит
// другая служба со своим сертификатом: проверка идет на адрес, где сертификат на этот домен
func TestProbeSiteOwnAddr(t *testing.T) {
	port := freePort(t)
	withProbePort(t, port)
	tlsAt(t, "127.0.0.2:"+port, "other.example.net", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
	}))
	tlsAt(t, "127.0.0.3:"+port, "site.example.com", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != "site.example.com:"+port {
			w.WriteHeader(421)
			return
		}
		_, _ = w.Write([]byte("hello ok"))
	}))
	ownIPs = func() []net.IP { return []net.IP{net.ParseIP("127.0.0.2"), net.ParseIP("127.0.0.3")} }
	hostIPs = func(string) []net.IP { return nil }
	r := probeSite(siteProbe{ID: 7, URL: "https://site.example.com:" + port + "/", IgnoreTLS: true, KeywordUp: "ok"})
	if r.Code != 200 || r.Via != "addr:127.0.0.3" || !r.KwUpFound || r.CertExpires == 0 {
		t.Fatalf("%+v", r)
	}
	// домен резолвится в адрес сервера - он первый, даже без сертификата на имя
	hostIPs = func(string) []net.IP { return []net.IP{net.ParseIP("127.0.0.2")} }
	if r := probeSite(siteProbe{URL: "http://site.example.com:" + port + "/"}); r.Via != "addr:127.0.0.2" {
		t.Fatalf("dns order: %+v", r)
	}
}

// белый список caddy рвет соединение после TLS: бэкенд из конфига прокси спрашивается напрямую с
// тем же Host, а сертификат снимается с самого прокси
func TestProbeSiteProxyBypass(t *testing.T) {
	port := freePort(t)
	withProbePort(t, port)
	tlsAt(t, "127.0.0.4:"+port, "site.example.com", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hj, _ := w.(http.Hijacker)
		c, _, _ := hj.Hijack()
		c.Close() // как caddy abort
	}))
	var gotHost string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHost = r.Host
		w.WriteHeader(204)
	}))
	defer backend.Close()
	ownIPs = func() []net.IP { return []net.IP{net.ParseIP("127.0.0.4")} }
	hostIPs = func(string) []net.IP { return nil }
	proxyRoutesLoad = func() []proxyRoute {
		return []proxyRoute{{hosts: []string{"site.example.com"}, proxy: "caddy", scheme: "http",
			addr: strings.TrimPrefix(backend.URL, "http://"), name: "container app-1:80"}}
	}
	r := probeSite(siteProbe{ID: 3, URL: "https://site.example.com:" + port + "/x?y=1", IgnoreTLS: true})
	if r.Code != 204 || r.Via != "proxy:caddy container app-1:80" || r.CertExpires == 0 || r.Error != "" {
		t.Fatalf("%+v", r)
	}
	if gotHost != "site.example.com:"+port {
		t.Fatalf("host %q", gotHost)
	}
	// прокси не знает домена - остается обрыв с адреса сервера
	proxyRoutesMu.Lock()
	proxyRoutesCache = nil
	proxyRoutesMu.Unlock()
	proxyRoutesLoad = func() []proxyRoute { return nil }
	if r := probeSite(siteProbe{URL: "https://other.example.com:" + port + "/", IgnoreTLS: true}); !probeDropped(r.Error) || r.Via != "addr:127.0.0.4" {
		t.Fatalf("no route: %+v", r)
	}
}
