package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestLocalProbeTarget(t *testing.T) {
	for _, p := range []siteProbe{
		{URL: "http://example.com/"},
		{URL: "https://example.com/health", Method: "head"},
		{URL: "https://example.com:443/", Method: "GET"},
		{URL: "http://example.com:80/x"},
	} {
		if err := localProbeTargetOK(p); err != nil {
			t.Fatalf("%+v: %v", p, err)
		}
	}
	for _, p := range []siteProbe{
		{URL: "http://example.com:2375/containers/db/json"},
		{URL: "https://example.com:2019/"},
		{URL: "http://example.com/containers/db/stop", Method: "POST"},
		{URL: "https://example.com/", Method: "PUT"},
		{URL: "ftp://example.com/"},
	} {
		if err := localProbeTargetOK(p); err == nil {
			t.Fatalf("accepted %+v", p)
		}
		if r := probeSite(p); r.Error == "" {
			t.Fatalf("probeSite went on with %+v", p)
		}
	}
}

// localDialer itself refuses any port but 80/443, whatever reaches it
func TestLocalDialerPort(t *testing.T) {
	hit := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hit = true }))
	defer srv.Close()
	port := srv.URL[strings.LastIndex(srv.URL, ":")+1:]
	r := probeSiteLocal(siteProbe{URL: "http://example.com:" + port + "/"})
	if hit || !strings.Contains(r.Error, "80 и 443") {
		t.Fatalf("hit=%v err=%q", hit, r.Error)
	}
	if _, err := probeRequest(siteProbe{Method: "DELETE"}, "http://example.com/"); err == nil {
		t.Fatal("DELETE accepted")
	}
}

func TestScrubCmdline(t *testing.T) {
	cases := map[string]string{
		"mysql -uroot -pfakepass shop":               "mysql -uroot -p*** shop",
		"/usr/bin/mysqldump -h db -pfakepass --all":  "/usr/bin/mysqldump -h db -p*** --all",
		"sshpass -p fakepass ssh host":               "sshpass -p *** ssh host",
		"redis-cli -h 127.0.0.1 -a fakepass ping":    "redis-cli -h 127.0.0.1 -a *** ping",
		"app --password fakepass --port 80":          "app --password *** --port 80",
		"app --password=fakepass":                    "app --password=***",
		"redis-server *:6379 --requirepass fakepass": "redis-server *:6379 --requirepass *** ",
		"psql postgres://u:fakepass@db/x":            "psql postgres://u:***@db/x",
		"ssh -p 2221 host":                           "ssh -p 2221 host",
		"nginx: worker process":                      "nginx: worker process",
		"mysql -h db -P 3306":                        "mysql -h db -P 3306",
	}
	for in, want := range cases {
		if got := scrubCmdline(in); strings.TrimSpace(got) != strings.TrimSpace(want) {
			t.Errorf("%q -> %q, want %q", in, got, want)
		}
	}
}

func TestScrubLog(t *testing.T) {
	id := "3f4e5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f"
	in := "sudo: u : COMMAND=/usr/bin/mysql -uroot -pfakepass db\n" +
		"app[1]: login password=fakepass ok\n" +
		"dockerd: container " + id + " started\n" +
		"x: redis-cli -a fakepass info\n"
	got := scrubLog(in)
	if strings.Contains(got, "fakepass") {
		t.Fatalf("secret left:\n%s", got)
	}
	if !strings.Contains(got, id) {
		t.Fatalf("container id masked:\n%s", got)
	}
}

func TestBackupSecrets(t *testing.T) {
	for _, s := range []string{"fakepass", "fake-token-example", "AbC123+/=_.,:@%-x"} {
		if !backupSecretRe.MatchString(s) {
			t.Fatalf("rejected %q", s)
		}
	}
	for _, s := range []string{"", "short", `fake"pass`, "fake$pass1", "fake;pass1", "fake pass1", "~fakepass", "fake`pass`", `fake\pass1`, "fakepass\nx=y"} {
		if backupSecretRe.MatchString(s) {
			t.Fatalf("accepted %q", s)
		}
	}
	for _, u := range []string{
		"rest:http://node-a:fakepass@203.0.113.7:64100/node-a",
		"rest:https://node-a:fakepass@backup.example.com:64101/node-a/",
		"rest:https://node-a:fakepass@[2001:db8::7]:64101/node-a",
	} {
		if !backupRepoURLRe.MatchString(u) {
			t.Fatalf("rejected %q", u)
		}
	}
	for _, u := range []string{
		"rest:http://node-a:fake$pass@203.0.113.7:64100/node-a",
		"rest:http://203.0.113.7:64100/a;id",
		"rest:http://203.0.113.7:64100/a b",
		"sftp:203.0.113.7:/a",
		`rest:http://203.0.113.7:64100/a"`,
	} {
		if backupRepoURLRe.MatchString(u) {
			t.Fatalf("accepted %q", u)
		}
	}
}

func TestProvisionClientLines(t *testing.T) {
	good := backupCommand{Action: "provision_client", Name: "node-a", Hpass: "fake-token-example",
		Repopass: "fakepass", ClientIP: "203.0.113.5"}
	if _, ok := backupServerLines(good); !ok {
		t.Fatal("rejected a good request")
	}
	for _, mut := range []func(*backupCommand){
		func(c *backupCommand) { c.Repopass = `a";touch /root/x;"` },
		func(c *backupCommand) { c.Repopass = "" },
		func(c *backupCommand) { c.Hpass = "fake:pass1" },
		func(c *backupCommand) { c.Hpass = "short" },
		func(c *backupCommand) { c.Name = ".." },
		func(c *backupCommand) { c.Name = ".htpasswd" },
		func(c *backupCommand) { c.Name = "-x" },
	} {
		c := good
		mut(&c)
		if _, ok := backupServerLines(c); ok {
			t.Fatalf("accepted %+v", c)
		}
	}
	if _, ok := backupServerLines(backupCommand{Action: "get_client_creds", Name: "."}); ok {
		t.Fatal("get_client_creds accepted .")
	}
}

func TestBackupHostAllowed(t *testing.T) {
	old := backupAllowFile
	defer func() { backupAllowFile = old }()
	backupAllowFile = filepath.Join(t.TempDir(), "backup-allow")
	u := "rest:https://node-a:fakepass@203.0.113.7:64101/node-a"
	if ok, _ := backupHostAllowed(u); !ok {
		t.Fatal("no file - the helper decides")
	}
	os.WriteFile(backupAllowFile, []byte("# servers\n198.51.100.2\n203.0.113.7  # main\n"), 0o644)
	if ok, why := backupHostAllowed(u); !ok {
		t.Fatalf("listed host refused: %s", why)
	}
	if ok, why := backupHostAllowed("rest:https://node-a:fakepass@[2001:db8::7]:64101/node-a"); ok || !strings.Contains(why, "2001:db8::7") {
		t.Fatalf("unlisted host: %v %q", ok, why)
	}
}

func TestDumpDirSystem(t *testing.T) {
	for _, d := range []string{"/backup", "/data/dumps", "/mnt/backup", "/home/app/dumps", "/srv", "/opt/dumps", "/var/backups"} {
		if !dumpDirOK(d) {
			t.Fatalf("rejected %s", d)
		}
	}
	for _, d := range []string{"/", "/var", "/var/lib", "/var/lib/", "/var/lib/mysql", "/etc", "/usr/local", "/tmp",
		"/root", "/home", "/lib65/kervax", "/boot", "/var/log/x", "/run/x", "/proc", "/a b", "/a;b"} {
		if dumpDirOK(d) {
			t.Fatalf("accepted %s", d)
		}
	}
}

func TestManifestFresh(t *testing.T) {
	now := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	m := manifest{Version: "2.52", ReleasedAt: "2026-10-20T09:15:00.123456+00:00"}
	if err := manifestFresh(m, now); err != nil {
		t.Fatal(err)
	}
	for _, ra := range []string{"", "yesterday", "2026-05-01T00:00:00+00:00", "2026-10-30T00:00:00+00:00"} {
		m.ReleasedAt = ra
		if manifestFresh(m, now) == nil {
			t.Fatalf("accepted released_at %q", ra)
		}
	}
}

func TestPanelURL(t *testing.T) {
	for _, u := range []string{"https://panel.example.com", "https://panel.example.com:8443/", "http://127.0.0.1:8000", "http://localhost:8000"} {
		if err := panelURLOK(u); err != nil {
			t.Fatalf("%s: %v", u, err)
		}
	}
	for _, u := range []string{"http://panel.example.com", "http://203.0.113.9:8000", "ftp://panel.example.com", "panel.example.com"} {
		if panelURLOK(u) == nil {
			t.Fatalf("accepted %s", u)
		}
	}
}

// a newline inside a value would add keys of its own to the request (the last one wins)
func TestSpoolRejectsNewlines(t *testing.T) {
	dir := t.TempDir()
	ok, out := spoolInFor(dir, dir, 1, []string{"action=provision", "repopass=fakepass\naction=deprovision"}, "test", time.Second)
	if ok || !strings.Contains(out, "недопустимый символ") {
		t.Fatalf("%v %q", ok, out)
	}
	if ents, _ := os.ReadDir(dir); len(ents) != 0 {
		t.Fatalf("a request was written: %v", ents)
	}
}
