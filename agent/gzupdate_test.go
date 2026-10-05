package main

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func gzipBytes(t *testing.T, b []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw, _ := gzip.NewWriterLevel(&buf, gzip.BestCompression)
	zw.Write(b)
	zw.Close()
	return buf.Bytes()
}

// panel - отдает сжатый бинарь с Range и HEAD, как FileResponse панели
func panel(gz []byte) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if gz == nil {
			http.NotFound(w, r)
			return
		}
		http.ServeContent(w, r, "agent.gz", time.Time{}, bytes.NewReader(gz))
	}))
}

func TestFetchCompressed(t *testing.T) {
	bin := []byte(strings.Repeat("kervax-agent binary ", 50000)) // ~1 МБ, жмется хорошо
	sum := sha256.Sum256(bin)
	art := artifact{SHA256: hex.EncodeToString(sum[:]), Size: len(bin)}
	gz := gzipBytes(t, bin)

	srv := panel(gz)
	defer srv.Close()
	got, err := fetchCompressed(srv.URL, "9.99", art)
	if err != nil || !bytes.Equal(got, bin) {
		t.Fatalf("сжатый: err=%v len=%d", err, len(got))
	}

	// старая панель: сжатого нет - это не ошибка, а сигнал качать несжатый
	old := panel(nil)
	defer old.Close()
	if _, err := fetchCompressed(old.URL, "9.99", art); !errors.Is(err, errNoCompressed) {
		t.Fatalf("старая панель: %v", err)
	}

	// архив другого бинаря: sha не сходится - отказ (зовущий возьмет несжатый)
	other := panel(gzipBytes(t, append([]byte("x"), bin[1:]...)))
	defer other.Close()
	if _, err := fetchCompressed(other.URL, "9.99", art); !errors.Is(err, errGzBroken) {
		t.Fatalf("чужой архив принят или не помечен битым: %v", err)
	}
}

func TestDownloadBinaryChoice(t *testing.T) {
	// Несжатый берем, только если сжатого нет или он не подошел. На обрыве сжатого агенты
	// 2.18-2.19 сразу переключались на несжатый, и на медленном канале попытки по очереди
	// тянули оба файла - половина времени уходила на тот, что потом выбрасывался.
	cleanupParts(t)
	defer cleanupParts(t)
	oldB, oldM := dlBackoff, dlBackoffMax
	dlBackoff, dlBackoffMax = time.Millisecond, 2*time.Millisecond
	defer func() { dlBackoff, dlBackoffMax = oldB, oldM }()

	bin := []byte(strings.Repeat("kervax-agent binary ", 20000))
	sum := sha256.Sum256(bin)
	art := artifact{SHA256: hex.EncodeToString(sum[:]), Size: len(bin)}
	gz := gzipBytes(t, bin)
	wrong := gzipBytes(t, append([]byte("x"), bin[1:]...))

	run := func(mode string) ([]byte, int32, error) {
		var plain int32
		mux := http.NewServeMux()
		mux.HandleFunc("/api/agent/download-gz/", func(w http.ResponseWriter, r *http.Request) {
			switch mode {
			case "none":
				http.NotFound(w, r)
			case "flaky": // размер честный, а сам файл не идет: 502 на каждый кусок
				if r.Method == http.MethodHead {
					w.Header().Set("Content-Length", strconv.Itoa(len(gz)))
					w.WriteHeader(http.StatusOK)
					return
				}
				w.WriteHeader(http.StatusBadGateway)
			case "broken":
				http.ServeContent(w, r, "a.gz", time.Time{}, bytes.NewReader(wrong))
			}
		})
		mux.HandleFunc("/api/agent/download/", func(w http.ResponseWriter, r *http.Request) {
			atomic.AddInt32(&plain, 1)
			http.ServeContent(w, r, "a", time.Time{}, bytes.NewReader(bin))
		})
		srv := httptest.NewServer(mux)
		defer srv.Close()
		got, err := downloadBinary(srv.URL, "9.98", art)
		return got, atomic.LoadInt32(&plain), err
	}

	if _, plain, err := run("flaky"); err == nil || plain != 0 {
		t.Fatalf("обрыв сжатого: err=%v, запросов несжатого %d (должна быть ошибка и ни одного)", err, plain)
	}
	for _, mode := range []string{"none", "broken"} {
		got, plain, err := run(mode)
		if err != nil || !bytes.Equal(got, bin) || plain == 0 {
			t.Fatalf("%s: err=%v len=%d несжатый=%d - должен был скачаться несжатый", mode, err, len(got), plain)
		}
	}
}

func TestGunzipLimited(t *testing.T) {
	big := bytes.Repeat([]byte{0}, 1<<20)
	gz := gzipBytes(t, big)
	// бомба: распаковывается больше подписанного размера - отказ, память не раздувается
	if _, err := gunzipLimited(gz, 1000); err == nil {
		t.Fatal("лишнее не замечено")
	}
	if _, err := gunzipLimited([]byte("not gzip"), 10); err == nil {
		t.Fatal("мусор принят")
	}
	if out, err := gunzipLimited(gz, len(big)); err != nil || len(out) != len(big) {
		t.Fatalf("норма: %v", err)
	}
}
