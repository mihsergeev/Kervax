package main

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
)

func allowRedirectTestPort(t *testing.T, rawURL string) {
	t.Helper()
	u, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	port := u.Port()
	localProbePorts[port] = true
	t.Cleanup(func() { delete(localProbePorts, port) })
}

func TestLocalSiteRedirectFinalResponse(t *testing.T) {
	final := 0
	var gotHost, gotMethod string
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/start" {
			w.Header().Set("Location", "/app")
			w.WriteHeader(http.StatusPermanentRedirect)
			return
		}
		if r.URL.Path == "/unresolved" {
			w.WriteHeader(http.StatusPermanentRedirect)
			return
		}
		final++
		gotHost = r.Host
		gotMethod = r.Method
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer s.Close()
	allowRedirectTestPort(t, s.URL)
	u, _ := url.Parse(s.URL)
	p := siteProbe{URL: "http://site.example.test:" + u.Port() + "/start", ExpectedStatus: "200-399"}
	r := probeSiteLocal(p)
	if r.Code != http.StatusServiceUnavailable || r.Error != "" || final != 1 || gotHost != "site.example.test:"+u.Port() {
		t.Fatalf("final 503: %+v, requests=%d, host=%q", r, final, gotHost)
	}
	p.Method = http.MethodHead
	r = probeSiteLocal(p)
	if r.Code != http.StatusServiceUnavailable || r.Error != "" || gotMethod != http.MethodHead {
		t.Fatalf("HEAD redirect: %+v, method=%q", r, gotMethod)
	}
	p.ExpectedStatus = "308"
	r = probeSiteLocal(p)
	if r.Code != http.StatusPermanentRedirect || r.Error != "" || final != 2 {
		t.Fatalf("intentional redirect: %+v, requests=%d", r, final)
	}
	p.URL = "http://site.example.test:" + u.Port() + "/unresolved"
	p.ExpectedStatus = "200-399"
	if r := probeSiteLocal(p); r.Code != http.StatusPermanentRedirect || r.Error == "" {
		t.Fatalf("redirect without final response: %+v", r)
	}
}

func TestLocalSiteRedirectSuccessAndFailures(t *testing.T) {
	otherHits := 0
	lookalikeHits := 0
	var otherLocation, lookalikeLocation, asciiCaseLocation string
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/ok":
			http.Redirect(w, r, "/healthy", http.StatusPermanentRedirect)
		case "/ascii-case":
			http.Redirect(w, r, asciiCaseLocation, http.StatusPermanentRedirect)
		case "/healthy":
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("app ready"))
		case "/loop":
			http.Redirect(w, r, "/loop", http.StatusPermanentRedirect)
		case "/external":
			http.Redirect(w, r, otherLocation, http.StatusPermanentRedirect)
		case "/lookalike":
			http.Redirect(w, r, lookalikeLocation, http.StatusPermanentRedirect)
		case "/lookalike-target":
			lookalikeHits++
			w.WriteHeader(http.StatusOK)
		default:
			otherHits++
			w.WriteHeader(http.StatusOK)
		}
	}))
	defer s.Close()
	allowRedirectTestPort(t, s.URL)
	u, _ := url.Parse(s.URL)
	base := "http://site.example.test:" + u.Port()
	otherLocation = "http://other.example.test:" + u.Port() + "/target"
	lookalikeLocation = "http://ſite.example.test:" + u.Port() + "/lookalike-target"
	asciiCaseLocation = "http://SITE.EXAMPLE.TEST:" + u.Port() + "/healthy"
	if r := probeSiteLocal(siteProbe{URL: base + "/ok", ExpectedStatus: "200", KeywordUp: "ready"}); r.Code != 200 || r.Error != "" || !r.KwUpFound {
		t.Fatalf("final 200: %+v", r)
	}
	if r := probeSiteLocal(siteProbe{URL: base + "/ascii-case", ExpectedStatus: "200"}); r.Code != 200 || r.Error != "" {
		t.Fatalf("ASCII hostname case: %+v", r)
	}
	if r := probeSiteLocal(siteProbe{URL: base + "/loop"}); r.Error == "" || r.Code != 0 {
		t.Fatalf("loop accepted: %+v", r)
	}
	if r := probeSiteLocal(siteProbe{URL: base + "/external", AuthUser: "fixture-user", Headers: `{"X-Fixture":"fixture-value"}`}); r.Error == "" || r.Code != 0 || otherHits != 0 {
		t.Fatalf("cross-host redirect accepted: %+v, other hits=%d", r, otherHits)
	}
	if r := probeSiteLocal(siteProbe{URL: base + "/lookalike", AuthUser: "fixture-user", AuthPass: "fixture-pass"}); r.Error == "" || r.Code != 0 || lookalikeHits != 0 {
		t.Fatalf("lookalike redirect accepted: %+v, lookalike hits=%d", r, lookalikeHits)
	}
}

func TestLocalSiteUpgradeDoesNotForwardCredentials(t *testing.T) {
	tlsHits := 0
	tlsSite := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tlsHits++
		w.WriteHeader(http.StatusOK)
	}))
	defer tlsSite.Close()
	allowRedirectTestPort(t, tlsSite.URL)
	uTLS, _ := url.Parse(tlsSite.URL)
	front := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://site.example.test:"+uTLS.Port()+"/app", http.StatusPermanentRedirect)
	}))
	defer front.Close()
	allowRedirectTestPort(t, front.URL)
	uFront, _ := url.Parse(front.URL)
	base := "http://site.example.test:" + uFront.Port() + "/"
	if r := probeSiteLocal(siteProbe{URL: base, IgnoreTLS: true}); r.Code != 200 || r.Error != "" || tlsHits != 1 || r.CertExpires == 0 {
		t.Fatalf("safe upgrade: %+v, tls hits=%d", r, tlsHits)
	}
	if r := probeSiteLocal(siteProbe{URL: base, IgnoreTLS: true, AuthUser: "fixture-user", AuthPass: "fixture-pass"}); r.Error == "" || r.Code != 0 || tlsHits != 1 {
		t.Fatalf("credentialed upgrade accepted: %+v, tls hits=%d", r, tlsHits)
	}
}

func TestLocalSiteRedirectTLSFailure(t *testing.T) {
	brokenTLS := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer brokenTLS.Close()
	allowRedirectTestPort(t, brokenTLS.URL)
	uBroken, _ := url.Parse(brokenTLS.URL)
	front := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://site.example.test:"+uBroken.Port()+"/app", http.StatusPermanentRedirect)
	}))
	defer front.Close()
	allowRedirectTestPort(t, front.URL)
	uFront, _ := url.Parse(front.URL)
	r := probeSiteLocal(siteProbe{URL: "http://site.example.test:" + uFront.Port() + "/"})
	if r.Code != 0 || r.Error == "" {
		t.Fatalf("TLS failure accepted: %+v", r)
	}
}

func TestClusterRedirectStaysOnSelectedService(t *testing.T) {
	var hosts []string
	var authOK bool
	svc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hosts = append(hosts, r.Host)
		if r.URL.Path == "/start" {
			http.Redirect(w, r, "https://site.example.test/app", http.StatusPermanentRedirect)
			return
		}
		user, pass, ok := r.BasicAuth()
		authOK = ok && user == "fixture-user" && pass == "fixture-pass"
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("service ready"))
	}))
	defer svc.Close()
	u, _ := url.Parse(svc.URL)
	old, _ := kubeRoutes.Load().(*kubeRouteTable)
	kubeRoutes.Store(&kubeRouteTable{rules: map[string][]kubeRouteRule{
		"site.example.test": {{path: "", ns: "test", svc: "app", ip: u.Hostname(), port: mustPort(t, u.Port())}},
	}, tls: map[string]string{}})
	t.Cleanup(func() {
		if old != nil {
			kubeRoutes.Store(old)
		} else {
			kubeRoutes.Store(&kubeRouteTable{})
		}
	})
	r, ok := probeSiteCluster(siteProbe{URL: "https://site.example.test/start", KeywordUp: "ready", AuthUser: "fixture-user", AuthPass: "fixture-pass"})
	if !ok || r.Code != 200 || r.Error != "" || !r.KwUpFound || len(hosts) != 2 || hosts[0] != "site.example.test" || hosts[1] != "site.example.test" || !authOK {
		t.Fatalf("cluster redirect: %+v, ok=%v, hosts=%v, auth ok=%v", r, ok, hosts, authOK)
	}
	hosts = nil
	r, ok = probeSiteCluster(siteProbe{URL: "http://site.example.test/start"})
	if !ok || r.Code != 0 || !strings.Contains(r.Error, "шлюз") || len(hosts) != 1 {
		t.Fatalf("cluster HTTP-to-HTTPS bypass accepted: %+v, ok=%v, hosts=%v", r, ok, hosts)
	}
}

func TestProxyRedirectStaysOnSelectedBackend(t *testing.T) {
	var hosts []string
	otherHits := 0
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		otherHits++
		w.WriteHeader(http.StatusOK)
	}))
	defer other.Close()
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hosts = append(hosts, r.Host)
		if r.URL.Path == "/start" {
			http.Redirect(w, r, "/app", http.StatusPermanentRedirect)
			return
		}
		if r.URL.Path == "/unsafe" {
			http.Redirect(w, r, other.URL+"/private", http.StatusPermanentRedirect)
			return
		}
		if r.URL.Path == "/upgrade" {
			http.Redirect(w, r, "https://site.example.test/app", http.StatusPermanentRedirect)
			return
		}
		if r.URL.Path == "/lookalike" {
			http.Redirect(w, r, "http://ſite.example.test/app", http.StatusPermanentRedirect)
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer backend.Close()
	proxyRoutesMu.Lock()
	oldCache, oldLoad := proxyRoutesCache, proxyRoutesLoad
	proxyRoutesCache = nil
	proxyRoutesLoad = func() []proxyRoute {
		return []proxyRoute{{hosts: []string{"site.example.test"}, proxy: "caddy", scheme: "http",
			addr: strings.TrimPrefix(backend.URL, "http://"), name: "app"}}
	}
	proxyRoutesMu.Unlock()
	t.Cleanup(func() {
		proxyRoutesMu.Lock()
		proxyRoutesCache, proxyRoutesLoad = oldCache, oldLoad
		proxyRoutesMu.Unlock()
	})
	r, ok := probeSiteProxy(siteProbe{URL: "http://site.example.test/start"}, "127.0.0.1")
	if !ok || r.Code != http.StatusServiceUnavailable || r.Error != "" || len(hosts) != 2 || hosts[0] != "site.example.test" || hosts[1] != "site.example.test" {
		t.Fatalf("proxy redirect: %+v, ok=%v, hosts=%v", r, ok, hosts)
	}
	r, ok = probeSiteProxy(siteProbe{URL: "http://site.example.test/unsafe"}, "127.0.0.1")
	if !ok || r.Code != 0 || r.Error == "" || otherHits != 0 {
		t.Fatalf("proxy escaped backend: %+v, ok=%v, other hits=%d", r, ok, otherHits)
	}
	hosts = nil
	r, ok = probeSiteProxy(siteProbe{URL: "http://site.example.test/upgrade"}, "127.0.0.1")
	if !ok || r.Code != 0 || !strings.Contains(r.Error, "шлюз") || len(hosts) != 1 {
		t.Fatalf("proxy HTTP-to-HTTPS bypass accepted: %+v, ok=%v, hosts=%v", r, ok, hosts)
	}
	hosts = nil
	r, ok = probeSiteProxy(siteProbe{URL: "http://site.example.test/lookalike", AuthUser: "fixture-user", AuthPass: "fixture-pass"}, "127.0.0.1")
	if !ok || r.Code != 0 || r.Error == "" || len(hosts) != 1 {
		t.Fatalf("proxy lookalike accepted: %+v, ok=%v, hosts=%v", r, ok, hosts)
	}
}

func mustPort(t *testing.T, port string) int {
	t.Helper()
	n, err := strconv.Atoi(port)
	if err != nil {
		t.Fatal(err)
	}
	return n
}
