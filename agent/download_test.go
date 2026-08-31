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
