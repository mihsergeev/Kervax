package main

// Докачка бинаря при обновлении. Проверяется то, из-за чего две ноды за DPI простояли
// на старой версии пять дней: канал рвал передачу примерно на середине, агент терял
// весь набранный кусок и следующая попытка начинала с нуля — вечный цикл на одном
// и том же месте.

import (
	"crypto/rand"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// сервер, отдающий данные по Range, но не больше limit байт за всё время жизни:
// имитирует канал, который умирает после N переданных байт.
func flakyServer(t *testing.T, data []byte, limit int) *httptest.Server {
	t.Helper()
	sent := 0
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		from, to := 0, len(data)-1
		if rng := r.Header.Get("Range"); strings.HasPrefix(rng, "bytes=") {
			parts := strings.SplitN(strings.TrimPrefix(rng, "bytes="), "-", 2)
			from, _ = strconv.Atoi(parts[0])
			if len(parts) == 2 && parts[1] != "" {
				to, _ = strconv.Atoi(parts[1])
			}
		}
		if from > to || to >= len(data) {
			w.WriteHeader(http.StatusRequestedRangeNotSatisfiable)
			return
		}
		if sent >= limit {
			// «канал кончился»: отвечаем так же, как прокси перед упавшим бэкендом
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		chunk := data[from : to+1]
		sent += len(chunk)
		w.Header().Set("Content-Range", "bytes "+strconv.Itoa(from)+"-"+strconv.Itoa(to)+"/"+strconv.Itoa(len(data)))
		w.WriteHeader(http.StatusPartialContent)
		w.Write(chunk)
	}))
}

// агент ищет каталог для .part рядом со своим бинарём — в тесте это сам тестовый бинарь,
// так что подчищаем за собой по маске.
func cleanupParts(t *testing.T) {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		return
	}
	matches, _ := filepath.Glob(filepath.Join(filepath.Dir(self), ".update-*.part"))
	for _, m := range matches {
		os.Remove(m)
	}
}

func TestDownloadResumesAcrossAttempts(t *testing.T) {
	cleanupParts(t)
	defer cleanupParts(t)
	// без этого тест ждёт настоящие паузы между неудачами — две минуты на ровном месте
	oldB, oldM := dlBackoff, dlBackoffMax
	dlBackoff, dlBackoffMax = time.Millisecond, 2*time.Millisecond
	defer func() { dlBackoff, dlBackoffMax = oldB, oldM }()

	data := make([]byte, 3<<20) // 3 МиБ
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}

	// Первая попытка: канал отдаёт чуть больше половины и умирает.
	half := len(data)/2 + 1000
	srv1 := flakyServer(t, data, half)
	defer srv1.Close()
	if _, err := httpGetChunked(srv1.URL, len(data), "9.9"); err == nil {
		t.Fatal("ожидали неудачу: канал отдал только половину")
	}

	// Огрызок должен остаться на диске — иначе следующая попытка начнёт с нуля,
	// а на таком канале до конца она не дойдёт никогда.
	pp := partPath("9.9", len(data))
	st, err := os.Stat(pp)
	if err != nil {
		t.Fatalf("недокачанное не сохранено (%v) — попытки не накапливаются", err)
	}
	if st.Size() == 0 || st.Size() >= int64(len(data)) {
		t.Fatalf("в огрызке %d байт из %d — ожидали часть", st.Size(), len(data))
	}
	got := st.Size()

	// Вторая попытка на таком же канале: она обязана ПРОДОЛЖИТЬ, а не начать заново.
	srv2 := flakyServer(t, data, len(data)) // теперь канал доживает до конца
	defer srv2.Close()
	bin, err := httpGetChunked(srv2.URL, len(data), "9.9")
	if err != nil {
		t.Fatalf("вторая попытка не доехала: %v", err)
	}
	if len(bin) != len(data) {
		t.Fatalf("скачано %d, ожидали %d", len(bin), len(data))
	}
	for i := range bin {
		if bin[i] != data[i] {
			t.Fatalf("байт %d не совпал — склейка кусков испортила файл", i)
		}
	}
	t.Logf("первая попытка набрала %d из %d байт, вторая продолжила с этого места", got, len(data))
}

func TestStalePartsAreCleaned(t *testing.T) {
	// Огрызки прошлых релизов и текущего убираются при старте, огрызок более новой
	// версии (недокачанное обновление) остается.
	cleanupParts(t)
	defer cleanupParts(t)
	old, cur, next := partPath("2.3", 100), partPath(version, 200), partPath("99.0", 300)
	for _, p := range []string{old, cur, next} {
		if err := os.WriteFile(p, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cleanStaleParts()
	for _, p := range []string{old, cur} {
		if _, err := os.Stat(p); err == nil {
			t.Fatalf("%s не убран", filepath.Base(p))
		}
	}
	if _, err := os.Stat(next); err != nil {
		t.Fatalf("огрызок новой версии убран, а это недокачанное обновление: %v", err)
	}
}

func TestPartFileIsPerVersionAndSize(t *testing.T) {
	// Огрызок от прошлого релиза не должен выдать себя за начало нового: иначе
	// склеенный файл не пройдёт sha256 и обновление будет отвергаться молча и вечно.
	a := partPath("2.1", 100)
	b := partPath("2.2", 100)
	c := partPath("2.2", 200)
	if a == b || b == c || a == c {
		t.Fatalf("имена огрызков совпали: %s / %s / %s", a, b, c)
	}
}

func TestPermanentVsTemporaryRejection(t *testing.T) {
	// Из-за отсутствия этого различия две ноды простояли на старой версии пять дней:
	// первая же сетевая неудача помечала версию отвергнутой НАВСЕГДА, и повторов не
	// было вовсе. Подпись действительно сама себя не починит, а оборванный канал — да.
	perm := permanent(errors.New("sha256 бинаря не совпал с подписанным"))
	if !isPermanent(perm) {
		t.Fatal("отказ по sha должен быть окончательным — иначе агент вечно тянет подделку")
	}
	if !isPermanent(fmt.Errorf("скачивание: %w", perm)) {
		t.Fatal("обёрнутый окончательный отказ перестал быть окончательным")
	}
	tmp := fmt.Errorf("на 3473408/5714055 байт: %w", errors.New("HTTP 502 вместо 206"))
	if isPermanent(tmp) {
		t.Fatal("обрыв канала посчитан окончательным — нода больше не попробует обновиться")
	}
}

// rangeOf - запрошенный диапазон [from..to] (без Range - весь файл).
func rangeOf(r *http.Request, n int) (int, int) {
	from, to := 0, n-1
	if rng := r.Header.Get("Range"); strings.HasPrefix(rng, "bytes=") {
		parts := strings.SplitN(strings.TrimPrefix(rng, "bytes="), "-", 2)
		from, _ = strconv.Atoi(parts[0])
		if len(parts) == 2 && parts[1] != "" {
			to, _ = strconv.Atoi(parts[1])
		}
	}
	if to > n-1 {
		to = n - 1
	}
	return from, to
}

func TestDownloadThroughFreezingChannel(t *testing.T) {
	// Канал, как у web-a-prod за DPI: соединение отдает ~11 КБ тела и замерзает
	// навсегда. Раньше агент ждал минуту на каждом таком куске, выбрасывал пришедшее и
	// не опускался ниже 64 КБ - OTA шла час на везении. Теперь: замерзшее рвется по
	// тишине, пришедшее остается, кусок за три неудачи падает до 8 КБ и там и остается.
	cleanupParts(t)
	defer cleanupParts(t)
	oldB, oldM, oldI := dlBackoff, dlBackoffMax, dlIdle
	dlBackoff, dlBackoffMax, dlIdle = time.Millisecond, 2*time.Millisecond, 100*time.Millisecond
	defer func() { dlBackoff, dlBackoffMax, dlIdle = oldB, oldM, oldI }()

	data := make([]byte, 200<<10)
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	total, frozen := 0, 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		from, to := rangeOf(r, len(data))
		chunk := data[from : to+1]
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", from, to, len(data)))
		w.Header().Set("Content-Length", strconv.Itoa(len(chunk)))
		w.WriteHeader(http.StatusPartialContent)
		mu.Lock()
		total++
		big := len(chunk) > 12<<10
		if big {
			frozen++
		}
		mu.Unlock()
		if big {
			w.Write(chunk[:11<<10])
			w.(http.Flusher).Flush()
			<-r.Context().Done() // замерзло: больше ни байта, пока клиент не уйдет
			return
		}
		w.Write(chunk)
	}))
	defer srv.Close()

	start := time.Now()
	bin, err := httpGetChunked(srv.URL, len(data), "9.97")
	if err != nil {
		t.Fatalf("не доехало: %v", err)
	}
	if len(bin) != len(data) {
		t.Fatalf("скачано %d, ожидали %d", len(bin), len(data))
	}
	for i := range bin {
		if bin[i] != data[i] {
			t.Fatalf("байт %d не совпал - склейка с оборванными кусками испортила файл", i)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	// 1 МБ -> 128 КБ -> 16 КБ замерзают, дальше только 8 КБ: три замерзших и ~22 целых
	if frozen > 3 {
		t.Fatalf("замерзших запросов %d - кусок не опускается до размера, который проходит", frozen)
	}
	if total > 30 {
		t.Fatalf("запросов %d на 200 КБ - пришедшее из оборванных кусков выбрасывается?", total)
	}
	t.Logf("запросов %d, из них замерзли %d, за %s", total, frozen, time.Since(start).Round(time.Millisecond))
}

func TestFetchRangeChecksContentRange(t *testing.T) {
	// Кусок не с того места склеился бы в битый файл (sha не сойдется, обновление
	// отвергнется навсегда) - такой ответ не берем ни целиком, ни частью.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Range", "bytes 0-99/1000")
		w.WriteHeader(http.StatusPartialContent)
		w.Write(make([]byte, 100))
	}))
	defer srv.Close()
	b, _, err := fetchRange(&http.Client{Timeout: 5 * time.Second}, srv.URL, 500, 599)
	if err == nil || len(b) != 0 {
		t.Fatalf("кусок не с того байта принят: err=%v len=%d", err, len(b))
	}
}
