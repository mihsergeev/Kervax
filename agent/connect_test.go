package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
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

func resetConnect() {
	panelConnect.Store("")
	connectDownUntil.Store(0)
	connectWarned.Store(false)
}

// connect= - только IP: имя резолвилось бы мимо туннеля
func TestParseConnect(t *testing.T) {
	for in, want := range map[string]string{
		"10.254.0.1": "10.254.0.1", " 10.254.0.1 ": "10.254.0.1", "[fd00::1]": "fd00::1",
		"panel.example": "", "10.254.0.1:443": "", "": "",
	} {
		if got := parseConnect(in); got != want {
			t.Errorf("parseConnect(%q) = %q, want %q", in, got, want)
		}
	}
}

// helper gre-setup пишет и снимает строку connect= - агент берет только ее
func TestReadConnect(t *testing.T) {
	defer resetConnect()
	p := filepath.Join(t.TempDir(), "kervax-agent.conf")
	os.WriteFile(p, []byte("url=https://panel.example\ntoken=fake-token-example\nconnect=10.254.0.1\n"), 0o600)
	if got := readConnect(p); got != "10.254.0.1" {
		t.Fatalf("readConnect = %q", got)
	}
	os.WriteFile(p, []byte("url=https://panel.example\ntoken=fake-token-example\n"), 0o600)
	if got := readConnect(p); got != "" {
		t.Fatalf("без connect= readConnect = %q", got)
	}
	// конфиг не прочитался - адрес как был
	panelConnect.Store("10.254.0.5")
	if got := readConnect(filepath.Join(t.TempDir(), "nope")); got != "10.254.0.5" {
		t.Fatalf("нет файла: readConnect = %q", got)
	}
}

// Как curl --resolve: TCP на адрес подключения, а TLS, SNI и проверка сертификата - на имя
// панели. Имя тут не резолвится вовсе, сертификат выписан на него.
func TestPanelRequestGoesToTheConnectAddressWithTheNameForTLS(t *testing.T) {
	defer resetConnect()
	var sni, host string
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host = r.Host
		io.WriteString(w, "ok")
	}))
	srv.TLS = &tls.Config{GetConfigForClient: func(h *tls.ClientHelloInfo) (*tls.Config, error) {
		sni = h.ServerName
		return nil, nil
	}}
	srv.StartTLS()
	defer srv.Close()
	_, port, _ := net.SplitHostPort(srv.Listener.Addr().String())
	// у сертификата httptest имя example.com; самой example.com по этому порту ничего нет
	name := "example.com:" + port
	old := dialTarget
	dialTarget = name
	defer func() { dialTarget = old }()
	panelConnect.Store("127.0.0.1")

	pool := x509.NewCertPool()
	pool.AddCert(srv.Certificate())
	tr := panelTransport(false)
	tr.TLSClientConfig = &tls.Config{RootCAs: pool}
	resp, err := (&http.Client{Timeout: 5 * time.Second, Transport: tr}).Get("https://" + name + "/api/agent/manifest")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(b) != "ok" || sni != "example.com" || host != name {
		t.Fatalf("ответ %q, SNI %q, Host %q", b, sni, host)
	}
	if connectDown() {
		t.Fatal("адрес ответил, а помечен неотвечающим")
	}
}

// Туннель не отвечает - соединение идет напрямую по имени, адрес помечается неотвечающим на
// connectRetry, и следующие соединения его не ждут
func TestPanelDialFallsBackToDirect(t *testing.T) {
	defer resetConnect()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	_, port, _ := net.SplitHostPort(ln.Addr().String())
	old := dialTarget
	dialTarget = "localhost:" + port
	defer func() { dialTarget = old }()
	// на 127.0.0.2 этот порт никто не слушает: отказ сразу
	panelConnect.Store("127.0.0.2")
	c, err := panelDial(context.Background(), "tcp", "localhost:"+port)
	if err != nil {
		t.Fatalf("напрямую не соединился: %v", err)
	}
	c.Close()
	if !connectDown() {
		t.Fatal("неответивший адрес не помечен")
	}
	start := time.Now()
	c, err = panelDial(context.Background(), "tcp", "localhost:"+port)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	if time.Since(start) > time.Second {
		t.Fatal("второе соединение снова ждало неотвечающий адрес")
	}
	// не панель (прокси, другой адрес) - адрес подключения не при делах
	connectDownUntil.Store(0)
	c, err = panelDial(context.Background(), "tcp", "127.0.0.1:"+port)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	if connectDown() {
		t.Fatal("соединение не к панели тронуло адрес подключения")
	}
}

// Замер: дошел ли кусок целиком одним соединением
func TestProbeOnce(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n, _ := strconv.Atoi(r.URL.Query().Get("n"))
		if r.Header.Get("Authorization") != "Bearer fake-token-example" || r.Header.Get("Accept-Encoding") != "identity" {
			w.WriteHeader(401)
			return
		}
		w.Header().Set("Content-Length", strconv.Itoa(n))
		if n > netProbeSmall {
			io.WriteString(w, strings.Repeat("x", n/2)) // оборвалось на половине
			return
		}
		io.WriteString(w, strings.Repeat("x", n))
	}))
	defer srv.Close()
	if !probeOnce(srv.URL, "fake-token-example", netProbeSmall) {
		t.Fatal("мелкий кусок не засчитан")
	}
	if probeOnce(srv.URL, "fake-token-example", netProbeBig) {
		t.Fatal("оборванный кусок засчитан")
	}
	if probeOnce(srv.URL, "wrong", netProbeSmall) {
		t.Fatal("отказ панели засчитан")
	}
}

func TestProbeRingKeepsTheLastAndResetsOnANewPath(t *testing.T) {
	var r probeRing
	var p *netProbe
	for i := 0; i < netProbeKeep+2; i++ {
		p = r.add("", true, i == 0, int64(i))
	}
	if p.SmallN != netProbeKeep || p.SmallOK != netProbeKeep || p.BigN != netProbeKeep || p.BigOK != 0 || p.BigKB != 100 {
		t.Fatalf("замеры %+v", *p)
	}
	p = r.add("10.254.0.1", true, true, 9)
	if p.SmallN != 1 || p.BigOK != 1 {
		t.Fatalf("после смены пути %+v", *p)
	}
	if s := fmt.Sprint(p.At); s != "9" {
		t.Fatal(s)
	}
}
