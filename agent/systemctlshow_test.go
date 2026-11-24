//go:build linux

package main

import (
	"os"
	"path/filepath"
	"testing"
)

// systemd 219 (CentOS 7) не знает `show --value`: значение берется из строки Key=value,
// а с флагом вызов падал и свойство всегда было пустым
func TestSystemctlShowWithoutValueFlag(t *testing.T) {
	dir := t.TempDir()
	stub := `#!/bin/sh
for a in "$@"; do
  [ "$a" = --value ] && { echo "systemctl: unrecognized option '--value'" >&2; exit 1; }
done
[ "$1 $2 $3 $4" = "show b.service -p Result" ] && echo Result=success
[ "$1 $2 $3 $4" = "show b.service -p ExecMainExitTimestampMonotonic" ] && echo ExecMainExitTimestampMonotonic=123456
exit 0
`
	if err := os.WriteFile(filepath.Join(dir, "systemctl"), []byte(stub), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	for prop, want := range map[string]string{
		"Result":                         "success",
		"ExecMainExitTimestampMonotonic": "123456",
		"InvocationID":                   "", // свойства нет (systemd < 232) - пусто, а не чужая строка
	} {
		if got := systemctlShow("b.service", prop); got != want {
			t.Errorf("systemctlShow(%s) = %q, ждали %q", prop, got, want)
		}
	}
}
