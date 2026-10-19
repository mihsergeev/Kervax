package main

import (
	"os"
	"path/filepath"
	"testing"
)

// ESM Infra Ubuntu Pro: подключен, когда в его файле есть живая строка с esm.ubuntu.com
func TestEsmInfra(t *testing.T) {
	dir := t.TempDir()
	list := filepath.Join(dir, "ubuntu-esm-infra.list")
	src := filepath.Join(dir, "ubuntu-esm-infra.sources")
	if esmInfraIn(list, src) {
		t.Fatal("no files - no ESM")
	}
	os.WriteFile(list, []byte("# Written by ubuntu-pro-client\n# deb https://esm.ubuntu.com/infra/ubuntu focal-infra-security main\n"), 0o644)
	if esmInfraIn(list, src) {
		t.Fatal("commented line is not ESM")
	}
	os.WriteFile(list, []byte("# Written by ubuntu-pro-client\ndeb https://esm.ubuntu.com/infra/ubuntu focal-infra-security main\n"), 0o644)
	if !esmInfraIn(list, src) {
		t.Fatal("deb line is ESM")
	}
	os.Remove(list)
	os.WriteFile(src, []byte("Types: deb\nURIs: https://esm.ubuntu.com/infra/ubuntu\nSuites: noble-infra-security noble-infra-updates\nComponents: main\n"), 0o644)
	if !esmInfraIn(list, src) {
		t.Fatal("deb822 sources is ESM")
	}
}
