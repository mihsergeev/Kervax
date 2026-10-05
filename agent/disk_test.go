package main

import (
	"syscall"
	"testing"
)

func TestDiskOf(t *testing.T) {
	// ext4 на 100 ГиБ: блок 4 КиБ, свободно 30 ГиБ, из них 5 ГиБ - резерв root
	st := syscall.Statfs_t{Bsize: 4096, Blocks: 26214400, Bfree: 7864320, Bavail: 6553600,
		Files: 6553600, Ffree: 6000000}
	d := diskOf("/", &st)
	if d.Total != 100<<30 || d.Used != 70<<30 || d.Avail != 25<<30 {
		t.Fatalf("место: %+v", d)
	}
	if d.Inodes != 6553600 || d.InodesUsed != 553600 {
		t.Fatalf("inode: %+v", d)
	}
	// btrfs не считает inode: полей нет, а не "0 из 0"
	st = syscall.Statfs_t{Bsize: 4096, Blocks: 1000, Bfree: 500, Bavail: 500}
	if d := diskOf("/data", &st); d.Inodes != 0 || d.InodesUsed != 0 {
		t.Fatalf("btrfs: %+v", d)
	}
}
