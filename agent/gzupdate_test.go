package main

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
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
	if _, err := fetchCompressed(other.URL, "9.99", art); err == nil || errors.Is(err, errNoCompressed) {
		t.Fatalf("чужой архив принят: %v", err)
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
