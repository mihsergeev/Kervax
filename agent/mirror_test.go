package main

import (
	"os"
	"os/exec"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

const testMirrorKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE1B7YqQzg7XNLJ2sx2hrF9SolyUDQG5d6BuG9qxn2rh"

// Основному серверу бэкапов: ключ резервного, его адрес и репозитории; пустой список убирает доступ
func TestMirrorAllowLines(t *testing.T) {
	got, ok := backupServerLines(backupCommand{Action: "mirror_allow", Name: "mirror-a", ClientIP: "203.0.113.7",
		PubKey: testMirrorKey, Repos: []string{"web-a", "db.b"}})
	want := []string{"action=mirror_allow", "name=mirror-a", "client_ip=203.0.113.7", "pubkey=" + testMirrorKey,
		"repo=web-a", "repo=db.b"}
	if !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q %v, want %q", got, ok, want)
	}
	got, ok = backupServerLines(backupCommand{Action: "mirror_allow", Name: "mirror-a"})
	if !ok || !reflect.DeepEqual(got, []string{"action=mirror_allow", "name=mirror-a"}) {
		t.Fatalf("revoke: got %q %v", got, ok)
	}
	for _, bad := range []backupCommand{
		{Action: "mirror_allow", Name: "../x", ClientIP: "203.0.113.7", PubKey: testMirrorKey, Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: testMirrorKey, Repos: []string{"../a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: "ssh-rsa AAAA", Repos: []string{"a"}},
		// опции authorized_keys или второй ключ не должны пройти вместе с ключом
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: `command="sh" ` + testMirrorKey, Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "203.0.113.7", PubKey: testMirrorKey + "\nssh-ed25519 x", Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "example.com", PubKey: testMirrorKey, Repos: []string{"a"}},
		{Action: "mirror_allow", Name: "m", ClientIP: "", PubKey: testMirrorKey, Repos: []string{"a"}},
	} {
		if got, ok := backupServerLines(bad); ok {
			t.Errorf("%+v: must be refused, got %q", bad, got)
		}
	}
}

// Резервному: по строке на основной сервер, или "забрать сейчас"; кривое не уходит в спул
func TestMirrorLines(t *testing.T) {
	hk := "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyExampleOnlyForTestsAAAAAAAAAAAA"
	got, ok := mirrorLines(backupCommand{Mode: "set", Sources: []mirrorSource{
		{Name: "bk-01", Host: "203.0.113.5", Port: 22, HostKey: hk, Repos: []string{"web-a", "db-b"}},
		{Name: "bk-02", Host: "backup.example.com", Port: 2222, HostKey: "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY=", Repos: nil},
	}})
	want := []string{"action=set",
		"source=bk-01 203.0.113.5 22 " + hk + " web-a,db-b",
		"source=bk-02 backup.example.com 2222 ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= -"}
	if !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q %v, want %q", got, ok, want)
	}
	if got, ok := mirrorLines(backupCommand{Mode: "pull"}); !ok || !reflect.DeepEqual(got, []string{"action=pull"}) {
		t.Fatalf("pull: got %q %v", got, ok)
	}
	src := mirrorSource{Name: "bk-01", Host: "203.0.113.5", Port: 22, HostKey: hk, Repos: []string{"a"}}
	mod := func(f func(*mirrorSource)) backupCommand {
		s := src
		f(&s)
		return backupCommand{Mode: "set", Sources: []mirrorSource{s}}
	}
	for _, bad := range []backupCommand{
		{Mode: "drop"},
		{Mode: "set", Sources: []mirrorSource{src, src}},
		mod(func(s *mirrorSource) { s.Name = "-x" }),
		mod(func(s *mirrorSource) { s.Host = "203.0.113.5 22" }),
		mod(func(s *mirrorSource) { s.Host = "-oProxyCommand=x" }),
		mod(func(s *mirrorSource) { s.Port = 0 }),
		mod(func(s *mirrorSource) { s.Port = 70000 }),
		mod(func(s *mirrorSource) { s.HostKey = "ssh-dss AAAAB3NzaC1kc3MAAACBAP" }),
		mod(func(s *mirrorSource) { s.HostKey = hk + " extra" }),
		mod(func(s *mirrorSource) { s.Repos = []string{"a,b"} }),
		mod(func(s *mirrorSource) { s.Repos = []string{"a b"} }),
	} {
		if got, ok := mirrorLines(bad); ok {
			t.Errorf("%+v: must be refused, got %q", bad, got)
		}
	}
	// перевод строки нигде не проходит: он дописал бы в запрос свои ключи
	for _, l := range want {
		if strings.ContainsAny(l, "\r\n") {
			t.Fatalf("newline in %q", l)
		}
	}
}

func TestMirrorPullRetainsMissingFiles(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("mirror helper requires GNU/Linux")
	}
	setup, err := os.ReadFile("mirror-setup.sh")
	if err != nil {
		t.Fatal(err)
	}
	_, body, ok := strings.Cut(string(setup), "<<'HELPER_EOF'\n")
	if !ok {
		t.Fatal("mirror helper heredoc not found")
	}
	body, _, ok = strings.Cut(body, "\ncase \"${1:-}\" in\n")
	if !ok {
		t.Fatal("mirror helper dispatch not found")
	}
	const test = `
DATA="$1/data"
SRC_D="$1/sources"
KNOWN_D="$1/known"
CURRENT="$1/current"
MIN_FREE_PCT=0
KEEP_DAYS=1
mkdir -p "$SRC_D" "$KNOWN_D"
printf 'host=127.0.0.1\nport=2222\nhostkey=ssh-ed25519 AAAAAAAAAAAAAAAAAAAA\n' > "$SRC_D/primary"
copy="$(copy_dir primary repo)"
st="$(state_dir primary repo)"
mkdir -p "$copy/snapshots" "$st"
printf 'config' > "$copy/config"
old="$(printf 'old' | sha256sum | cut -d' ' -f1)"
printf 'old' > "$copy/snapshots/$old"
printf 'snapshots/%s 1 1\n' "$old" > "$st/seen"
printf 'config 6 1\nsnapshots/%s 3 1\n' "$old" > "$st/list.prev"
printf 'src_newest=1\nlast_ok=1\n' > "$st/status"

# The real sandbox uses two users. Keep the fixture runnable as an unprivileged user.
install() {
  local -a args=()
  while [ "$#" -gt 0 ]; do
    case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
  done
  command install "${args[@]}"
}
chown() { :; }

# Simulate a successful SSH transfer that first omits a file, then sends the full listing.
new="$(printf 'new' | sha256sum | cut -d' ' -f1)"
systemd-run() {
  local stage="$DATA/.staging/primary--repo"
  mkdir -p "$stage/files/snapshots"
  printf 'config 6 2\nsnapshots/%s 3 2\n' "$new" > "$stage/list"
  printf 'snapshots/%s 3 2\nconfig 6 2\n' "$new" > "$stage/need"
  printf 'config' > "$stage/files/config"
  if [ "$complete" = 1 ]; then printf 'new' > "$stage/files/snapshots/$new"; fi
  touch "$stage/list.ok" "$stage/fetched"
}

complete=0
pull_one primary repo
test "$(kv "$st/status" err_code)" = integrity
test "$(kv "$st/status" src_newest)" = 1
test "$(kv "$st/status" last_ok)" = 1
test ! -e "$copy/snapshots/$new"
cmp -s "$copy/snapshots/$old" <(printf 'old')

complete=1
for run in 1 2; do
  pull_one primary repo
  test "$(kv "$st/status" src_newest)" = 2
  test "$(kv "$st/status" gone_files)" = 1
  test "$(kv "$st/status" snaps)" = 2
  cmp -s "$copy/snapshots/$old" <(printf 'old')
  cmp -s "$copy/snapshots/$new" <(printf 'new')
done
`
	cmd := exec.Command("bash", "-c", body+test, "mirror-test", t.TempDir())
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("mirror pull failed: %v\n%s", err, out)
	}
}
