package main

// Home verification only collects observations. Neither shell commands nor root
// actions come from a client. Privileged collectors use the narrow file protocol
// installed by recheck-setup, under the same NoNewPrivileges boundary as backups.
import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"syscall"
	"time"
)

type alertRecheckTask struct {
	ID      string `json:"id"`
	Source  string `json:"source"`
	DBStats bool   `json:"db_stats"`
}

var recheckID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var recheckRunning sync.Map
var recheckSlots = make(chan struct{}, 2)
var pkgCollectMu sync.Mutex

// Serialize publication with the normal report: a report built before this
// snapshot must finish before its verified result is accepted by the panel.
var reportCollectionMu sync.Mutex

func collectAndSend(url, token string, prev sample) (report, sample, config, error) {
	reportCollectionMu.Lock()
	defer reportCollectionMu.Unlock()
	r, cur := collect(prev)
	cfg, err := send(url, token, r)
	return r, cur, cfg, err
}

var recheckRoot = "/var/lib/kervax"

func rootRecheckReady() bool {
	p := filepath.Join(recheckRoot, "recheck-req")
	var fs syscall.Statfs_t
	if syscall.Statfs(p, &fs) != nil || fs.Flags&1 != 0 || syscall.Access(p, 3) != nil {
		return false
	}
	f, err := os.CreateTemp(p, ".ready-*")
	if err != nil {
		return false
	}
	f.Close()
	os.Remove(f.Name())
	return true
}

func rootRecheck(t alertRecheckTask) (map[string]json.RawMessage, error) {
	if !recheckID.MatchString(t.ID) {
		return nil, fmt.Errorf("invalid request")
	}
	// No symlink following, no replacing an already queued request. The root
	// result directory is not writable by the agent.
	p := filepath.Join(recheckRoot, "recheck-req", t.ID+".json")
	b, _ := json.Marshal(t)
	f, err := os.CreateTemp(filepath.Join(recheckRoot, "recheck-req"), ".pending-"+t.ID+".*")
	if err != nil {
		return nil, err
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	_, err = f.Write(b)
	f.Close()
	if err != nil {
		os.Remove(p)
		return nil, err
	}
	if err = os.Rename(tmp, p); err != nil {
		return nil, err
	}
	defer os.Remove(p)
	rp := filepath.Join(recheckRoot, "recheck-res", t.ID+".json")
	defer os.Remove(rp) // normally root-owned: expiry cleanup also runs in helper
	until := time.Now().Add(145 * time.Second)
	for time.Now().Before(until) {
		fi, err := os.Lstat(rp)
		if err == nil && fi.Mode().IsRegular() && fi.Size() <= 4_000_000 {
			b, err := os.ReadFile(rp)
			var out struct {
				ID       string                     `json:"id"`
				OK       bool                       `json:"ok"`
				Snapshot map[string]json.RawMessage `json:"snapshot"`
			}
			if err == nil && json.Unmarshal(b, &out) == nil && out.ID == t.ID && out.OK {
				return out.Snapshot, nil
			}
			return nil, fmt.Errorf("collector unavailable")
		}
		time.Sleep(200 * time.Millisecond)
	}
	return nil, fmt.Errorf("collector timeout")
}

func rawValue(v any) json.RawMessage { b, _ := json.Marshal(v); return b }

func freshDocker() (json.RawMessage, error) {
	d := collectDocker()
	if d == nil {
		return json.RawMessage(`{"present":false,"access":false,"containers":[]}`), nil
	}
	if !d.Access || !d.ContainersComplete {
		return nil, fmt.Errorf("docker inventory unavailable")
	}
	return rawValue(d), nil
}

func collectAlertRecheck(t alertRecheckTask) (map[string]json.RawMessage, error) {
	out := map[string]json.RawMessage{}
	switch t.Source {
	case "kube_expiry", "changes":
		return rootRecheck(t)
	case "software":
		pkgCollectMu.Lock()
		pk := collectPkgs(nil) // force the package database read; never pkgLatest
		if pk != nil {
			pkgLatest.Store(pk)
		}
		pkgCollectMu.Unlock()
		if pk == nil {
			return nil, fmt.Errorf("package inventory unavailable")
		}
		out["packages"] = rawValue(pk.items)
		out["pkgs"] = rawValue(pk.sum)
		dk, err := freshDocker()
		if err != nil {
			return nil, err
		}
		out["docker"] = dk
		out["db_stats"] = rawValue([]dbStat{})
		if t.DBStats {
			snap, err := rootRecheck(t)
			if err != nil {
				return nil, err
			}
			out["db_stats"] = snap["db_stats"]
		}
	case "docker":
		dk, err := freshDocker()
		if err != nil {
			return nil, err
		}
		out["docker"] = dk
	case "kube":
		out["kube"] = rawValue(collectKube())
	case "backup":
		snap, err := rootRecheck(t)
		if err != nil {
			return nil, err
		}
		var cfg struct {
			BackupConfig  json.RawMessage `json:"backup_config"`
			CustomBackups json.RawMessage `json:"custom_backups"`
		}
		if json.Unmarshal(snap["inspect"], &cfg) != nil || len(cfg.BackupConfig) == 0 || len(cfg.CustomBackups) == 0 {
			return nil, fmt.Errorf("backup inspection unavailable")
		}
		bi := collectBackup()
		if bi == nil {
			bi = &backupInfo{}
		}
		// Config fields use the exact backupInfo protocol. Never read credentials.
		if json.Unmarshal(cfg.BackupConfig, bi) != nil {
			return nil, fmt.Errorf("invalid backup inspection")
		}
		out["backup"] = rawValue(bi)
		out["extras"] = rawValue(map[string]json.RawMessage{"custom-backups": cfg.CustomBackups})
		out["db_engines"] = rawValue(collectDBEngines())
		out["db_stats"] = rawValue([]dbStat{})
		if t.DBStats {
			out["db_stats"] = snap["db_stats"]
		}
	case "mem":
		mi := meminfo()
		if mi["MemTotal"] == 0 {
			return nil, fmt.Errorf("memory unavailable")
		}
		out["mem_total"], out["mem_used"] = rawValue(mi["MemTotal"]), rawValue(mi["MemTotal"]-mi["MemAvailable"])
	case "disk":
		d := disks()
		if len(d) == 0 {
			return nil, fmt.Errorf("disk inventory unavailable")
		}
		out["disks"] = rawValue(d)
	case "conntrack":
		count, max := readConntrack()
		if max == 0 {
			return nil, fmt.Errorf("conntrack unavailable")
		}
		out["conntrack_count"], out["conntrack_max"] = rawValue(count), rawValue(max)
	case "temp":
		v, ok := readTempC()
		if !ok {
			return nil, fmt.Errorf("temperature unavailable")
		}
		out["cpu_temp"] = rawValue(v)
	default:
		return nil, fmt.Errorf("unsupported collector")
	}
	return out, nil
}

func runAlertRecheck(panelURL, token string, t alertRecheckTask) {
	if !recheckID.MatchString(t.ID) {
		return
	}
	if _, loaded := recheckRunning.LoadOrStore(t.ID, true); loaded {
		return
	}
	defer recheckRunning.Delete(t.ID)
	select {
	case recheckSlots <- struct{}{}:
		defer func() { <-recheckSlots }()
	default:
		return
	}
	snap, err := collectAlertRecheck(t)
	if snap == nil {
		snap = map[string]json.RawMessage{}
	}
	body, _ := json.Marshal(map[string]any{"id": t.ID, "ok": err == nil, "snapshot": snap})
	if len(body) > 4_000_000 {
		body, _ = json.Marshal(map[string]any{"id": t.ID, "ok": false, "snapshot": map[string]any{}})
	}
	reportCollectionMu.Lock()
	defer reportCollectionMu.Unlock()
	req, err := http.NewRequest("POST", panelURL+"/api/agent/alert-recheck-result", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := panelClient(15 * time.Second).Do(req)
	if err != nil {
		return
	} // SQL lease returns the task on a later poll
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
}
