package main

import (
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
	"unicode/utf8"
)

// Самообновление во время выполняющейся команды: exec ждет, пока ее результат дойдет до
// панели, а очередь команд во время слива не опрашивается. До 2.74 exec шел сразу, и команда,
// которую панель уже отметила running, пропадала вместе с результатом.
func TestSelfUpdateDrainDeliversCommandResult(t *testing.T) {
	var polls, results atomic.Int32
	var mu sync.Mutex
	var got []string
	panelSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/agent/commands":
			if polls.Add(1) == 1 {
				// команда, которая отвечает не сразу: результат уходит к панели медленно
				w.Write([]byte(`{"backup_commands":[{"id":7,"action":"set_paths","mode":"bad"}]}`))
				return
			}
			w.Write([]byte(`{}`))
		case "/api/agent/backup-result":
			time.Sleep(700 * time.Millisecond)
			var b struct {
				ID     int    `json:"id"`
				Output string `json:"output"`
			}
			json.NewDecoder(r.Body).Decode(&b)
			mu.Lock()
			got = append(got, b.Output)
			mu.Unlock()
			results.Add(1)
		}
	}))
	defer panelSrv.Close()
	defer tasks.resume()

	oldExec, oldTick := execFn, drainTick
	defer func() { execFn, drainTick = oldExec, oldTick }()
	drainTick = 10 * time.Millisecond
	var atExec int32 = -1
	execFn = func(argv0 string, argv []string, envv []string) error {
		atExec = results.Load()
		return errors.New("exec в тесте не нужен")
	}

	cl := &http.Client{Timeout: 5 * time.Second}
	if !tasks.poll() {
		t.Fatal("опрос не начался")
	}
	pollCommands(cl, panelSrv.URL+"/api/agent/commands", panelSrv.URL, "fake-token")
	tasks.done()

	p := trialPaths(filepath.Join(t.TempDir(), "kervax-agent"), "0123456789ab")
	err := execTrial(p, "9.99")
	if err == nil || !strings.Contains(err.Error(), "exec в тесте") {
		t.Fatalf("execTrial: %v", err)
	}
	if atExec != 1 {
		t.Fatalf("exec случился, когда до панели дошло результатов: %d (нужен 1)", atExec)
	}
	if polls.Load() != 1 {
		t.Fatalf("очередь опрашивали %d раз - во время слива ее трогать нельзя", polls.Load())
	}
	if len(got) != 1 || got[0] != "неизвестный режим" {
		t.Fatalf("результат команды: %q", got)
	}
	// exec не случился - попытки не было, маркер не остается; снова работаем
	if _, err := os.Stat(p.mark); !os.IsNotExist(err) {
		t.Fatal("маркер несостоявшегося exec остался")
	}
	if !tasks.poll() {
		t.Fatal("после неудачного exec очередь не опрашивается")
	}
	tasks.done()
	if !tasks.report() {
		t.Fatal("после неудачного exec отчет не идет")
	}
	tasks.done()
}

// Слив с ограничением: зависшая команда не держит обновление дольше drainMax, а отчет
// основного цикла после печати не идет (reportOnce даже не собирает его).
func TestTaskGateDrainDeadline(t *testing.T) {
	defer tasks.resume()
	oldTick := drainTick
	defer func() { drainTick = oldTick }()
	drainTick = 5 * time.Millisecond
	release := make(chan struct{})
	tasks.spawn(func() { <-release })
	start := time.Now()
	if left := tasks.drain(100 * time.Millisecond); left != 1 {
		t.Fatalf("не закончилось %d, нужна 1", left)
	}
	if d := time.Since(start); d < 100*time.Millisecond || d > 2*time.Second {
		t.Fatalf("слив шел %s", d)
	}
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	if _, _, _, _, ok := reportOnce(srv.URL, "fake-token", sample{}); ok || hits.Load() != 0 {
		t.Fatalf("отчет после печати слива: ok=%v, запросов %d", ok, hits.Load())
	}
	if tasks.poll() {
		t.Fatal("после слива взяли новую работу")
	}
	close(release)
	tasks.resume()
	if !tasks.report() {
		t.Fatal("после resume отчет не идет")
	}
	tasks.done()
	for i := 0; i < 100; i++ {
		tasks.mu.Lock()
		n := tasks.n
		tasks.mu.Unlock()
		if n == 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("счетчик задач не вернулся к нулю")
}

// Отчет во время слива несет draining: панель 2.74 команд в ответе не дает.
func TestReportCarriesDraining(t *testing.T) {
	defer tasks.resume()
	var last atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body io.Reader = r.Body
		if r.Header.Get("Content-Encoding") == "gzip" {
			body, _ = gzip.NewReader(r.Body)
		}
		var m map[string]any
		json.NewDecoder(body).Decode(&m)
		last.Store(m)
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	draining := func() any { m, _ := last.Load().(map[string]any); return m["draining"] }
	if _, err := send(srv.URL, "fake-token", report{}); err != nil || draining() != nil {
		t.Fatalf("обычный отчет: %v, draining=%v", err, draining())
	}
	tasks.drain(time.Second) // задач нет - сразу печать, флаг слива стоит
	if _, err := send(srv.URL, "fake-token", report{}); err != nil || draining() != true {
		t.Fatalf("отчет во время слива: %v, draining=%v", err, draining())
	}
	// проба: панель не снимает цель обновления, пока в отчете trial
	trialFlag := func() any { m, _ := last.Load().(map[string]any); return m["trial"] }
	trialFromV := func() any { m, _ := last.Load().(map[string]any); return m["trial_from"] }
	setTrial(&trialFiles{})
	defer setTrial(nil)
	trialMu.Lock()
	trialFrom = "2.74"
	trialMu.Unlock()
	defer func() { trialMu.Lock(); trialFrom = ""; trialMu.Unlock() }()
	if _, err := send(srv.URL, "fake-token", report{}); err != nil || trialFlag() != true || trialFromV() != "2.74" {
		t.Fatalf("отчет пробы: %v, trial=%v, trial_from=%v", err, trialFlag(), trialFromV())
	}
	setTrial(nil)
	if _, err := send(srv.URL, "fake-token", report{}); err != nil || trialFlag() != nil || trialFromV() != nil {
		t.Fatalf("отчет после пробы: %v, trial=%v, trial_from=%v", err, trialFlag(), trialFromV())
	}
}

type execDone struct{}

func trialSetup(t *testing.T) (self string, p trialFiles) {
	t.Helper()
	oldKey, oldExec, oldReports, oldAge := instKey, execFn, trialCommitReports, trialCommitAge
	t.Cleanup(func() {
		instKey, execFn, trialCommitReports, trialCommitAge = oldKey, oldExec, oldReports, oldAge
		setTrial(nil)
		rejectedVersion.Store("")
		updateRetryAt.Store(time.Time{})
		lastUpdateError.Store("")
		tasks.resume()
	})
	instKey = instanceKey("/etc/kervax-agent-test.conf")
	// exec удался: в жизни он не возвращается, здесь - паника, которую ловит runTrial
	execFn = func(string, []string, []string) error { panic(execDone{}) }
	trialCommitReports, trialCommitAge = 3, 0
	self = filepath.Join(t.TempDir(), "kervax-agent")
	if err := os.WriteFile(self, []byte("old binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	return self, trialPaths(self, instKey)
}

// runTrial - startTrial до exec (включительно); после него процесс «заменен»
func runTrial(t *testing.T, self, ver string, bin []byte) {
	t.Helper()
	defer func() {
		tasks.resume()
		if r := recover(); r != nil {
			if _, ok := r.(execDone); !ok {
				panic(r)
			}
			return
		}
		t.Fatal("exec не вызван")
	}()
	startTrial(self, ver, bin)
}

func readFile(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("%s: %v", p, err)
	}
	return string(b)
}

func shaOf(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// Новый бинарь не трогает штатный, пока проба не продержалась: упал - systemd поднимает
// штатный, то есть прежнюю версию, она считает неудачу и после trialMax больше эту версию не
// ставит. До 2.74 бинарь сразу заменял штатный, и откатить было нечем.
func TestSelfUpdateTrialRollsBackAfterCrashes(t *testing.T) {
	self, p := trialSetup(t)
	for try := 1; try <= trialMax; try++ {
		runTrial(t, self, "9.99", []byte("new binary"))
		if readFile(t, self) != "old binary" {
			t.Fatal("штатный бинарь заменен до конца пробы")
		}
		if readFile(t, p.bin) != "new binary" {
			t.Fatal("пробный бинарь не записан")
		}
		if st, err := os.Stat(p.prev); err != nil || st.Size() == 0 || st.Mode().Perm() != 0o755 {
			t.Fatalf(".prev: %v %v", st, err)
		}
		var m trialMark
		if json.Unmarshal([]byte(readFile(t, p.mark)), &m) != nil || m.Version != "9.99" || m.Tries != try ||
			m.From != version || m.SelfSHA != shaOf("old binary") {
			t.Fatalf("маркер: %+v", m)
		}
		// «упал»: systemd поднял штатный бинарь, он разбирает маркер
		trialOnStart(self, instKey)
		rej, _ := rejectedVersion.Load().(string)
		errText, _ := lastUpdateError.Load().(string)
		if !strings.Contains(errText, "не продержалась пробу") {
			t.Fatalf("причина не видна панели: %q", errText)
		}
		if try < trialMax {
			if rej != "" {
				t.Fatalf("после %d неудач версия уже отвергнута", try)
			}
			if at, _ := updateRetryAt.Load().(time.Time); at.IsZero() {
				t.Fatal("нет паузы перед повтором")
			}
		} else {
			if rej != "9.99" {
				t.Fatalf("после %d неудач версия не отвергнута: %q", try, rej)
			}
			if _, err := os.Stat(p.bin); !os.IsNotExist(err) {
				t.Fatal("отвергнутый пробный бинарь остался")
			}
		}
	}
	// другая версия начинает счет заново
	runTrial(t, self, "9.100", []byte("newer"))
	var m trialMark
	json.Unmarshal([]byte(readFile(t, p.mark)), &m)
	if m.Version != "9.100" || m.Tries != 1 {
		t.Fatalf("маркер новой версии: %+v", m)
	}
}

// Перезапуск во время слива (перезагрузка, вотчдог) - не попытка: маркер пишется после слива.
func TestSelfUpdateRestartDuringDrainIsNotATry(t *testing.T) {
	self, p := trialSetup(t)
	oldTick := drainTick
	defer func() { drainTick = oldTick }()
	drainTick = 5 * time.Millisecond
	release := make(chan struct{})
	tasks.spawn(func() { <-release })
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer func() { recover() }()
		startTrial(self, "9.99", []byte("new binary"))
	}()
	for i := 0; i < 200 && !tasks.isDraining(); i++ {
		time.Sleep(5 * time.Millisecond)
	}
	if !tasks.isDraining() {
		t.Fatal("слив не начался")
	}
	if _, err := os.Stat(p.mark); !os.IsNotExist(err) {
		t.Fatal("маркер пробы записан до конца слива")
	}
	// здесь агент перезапустили: штатный бинарь неудачи не видит
	trialOnStart(self, instKey)
	if e, _ := lastUpdateError.Load().(string); e != "" {
		t.Fatalf("перезапуск во время слива засчитан: %q", e)
	}
	close(release)
	<-done
	var m trialMark
	if json.Unmarshal([]byte(readFile(t, p.mark)), &m) != nil || m.Tries != 1 {
		t.Fatalf("маркер после слива: %+v", m)
	}
}

// Проба становится штатной только после trialCommitReports отчетов и trialCommitAge: паника
// через секунды после старта откатывается. Маркер убран, следующий старт ничего не откатывает.
func TestSelfUpdateTrialCommit(t *testing.T) {
	self, p := trialSetup(t)
	runTrial(t, self, version, []byte("new binary")) // пробный процесс - этот, его версия
	// процесс другого экземпляра (чужой ключ) этой пробой не становится
	trialOnStart(trialPaths(self, "0123456789ab").bin, instKey)
	if trialPending() {
		t.Fatal("пробный файл чужого экземпляра принят за свой")
	}
	// так стартует процесс, запущенный exec'ом из пробного бинаря
	trialOnStart(p.bin, instKey)
	if on, from := trialState(); !on || from != version {
		t.Fatalf("пробный процесс себя не узнал: %v, с версии %q", on, from)
	}
	if err := selfUpdate("http://127.0.0.1:1", "9.100"); err == nil || !strings.Contains(err.Error(), "на пробе") {
		t.Fatalf("обновление поверх непринятой пробы: %v", err)
	}
	trialCommitAge = time.Hour // процесс живет меньше часа
	for i := 0; i < 5; i++ {
		trialReportOK()
	}
	if !trialPending() || readFile(t, self) != "old binary" {
		t.Fatal("проба принята раньше trialCommitAge")
	}
	trialCommitAge = 0
	setTrial(&p) // счет отчетов заново
	trialReportOK()
	trialReportOK()
	if !trialPending() || readFile(t, self) != "old binary" {
		t.Fatal("проба принята раньше trialCommitReports отчетов")
	}
	trialReportOK()
	if trialPending() || readFile(t, self) != "new binary" {
		t.Fatal("штатный бинарь не заменен после пробы")
	}
	for _, f := range []string{p.bin, p.mark} {
		if _, err := os.Stat(f); !os.IsNotExist(err) {
			t.Fatalf("%s остался", f)
		}
	}
	if _, err := os.Stat(p.prev); err != nil {
		t.Fatal("нет копии прежнего бинаря")
	}
	trialOnStart(self, instKey)
	if v, _ := rejectedVersion.Load().(string); v != "" {
		t.Fatalf("принятая версия отвергнута: %q", v)
	}
	// маркер версии не новее текущей (проба принята, а маркер не успели убрать) - просто убираем
	os.WriteFile(p.mark, []byte(`{"version":"`+version+`","tries":3}`), 0o644)
	os.WriteFile(p.bin, []byte("x"), 0o755)
	trialOnStart(self, instKey)
	if _, err := os.Stat(p.mark); !os.IsNotExist(err) {
		t.Fatal("старый маркер остался")
	}
	if v, _ := rejectedVersion.Load().(string); v != "" {
		t.Fatalf("версия %q отвергнута по старому маркеру", v)
	}
}

// markedBin - содержимое бинаря с меткой версии v (как у агента 2.75+)
func markedBin(name, v string) string {
	return name + "\x00garbage\x00KERVAX_AGENT_VERSION=" + v + "\x00tail"
}

// Экземпляры с общим бинарем: пока шла проба A, штатный заменил экземпляр B версией не старше
// нашей. A поверх не пишет и пробу снимает без неудачи.
func TestSelfUpdateTrialDoesNotOverwriteOtherInstance(t *testing.T) {
	for _, other := range []string{version, "99.0"} {
		self, p := trialSetup(t)
		runTrial(t, self, version, []byte("trial A")) // версия пробы = version этого процесса
		trialOnStart(p.bin, instKey)
		byB := markedBin("committed by B", other)
		os.WriteFile(self, []byte(byB), 0o755)
		for i := 0; i < 3; i++ {
			trialReportOK()
		}
		if trialPending() {
			t.Fatalf("штатный %s: проба не снята", other)
		}
		if readFile(t, self) != byB {
			t.Fatalf("проба A записала поверх штатного %s, который поставил экземпляр B", other)
		}
		if _, err := os.Stat(p.mark); !os.IsNotExist(err) {
			t.Fatal("маркер снятой пробы остался")
		}
		if e, _ := lastUpdateError.Load().(string); e != "" {
			t.Fatalf("снятая проба засчитана как ошибка: %q", e)
		}
	}
}

// Гонка экземпляров с разными панелями (2.75): A поставил 2.79, пока шла проба 2.80 у B.
// Побеждает новейшая версия, а не первый коммит: B ставит свою поверх. В 2.74 B тихо снимал
// пробу, работал 2.80 из пробного файла, панель снимала цель, а после рестарта нода была 2.79.
// Бинарь без метки - агент до 2.75, он старше этого кода.
func TestTrialCommitNewestWins(t *testing.T) {
	for _, installed := range []string{markedBin("committed by A", "2.74"), markedBin("by A", "1.9.99"), "pre-2.75 binary"} {
		self, p := trialSetup(t)
		runTrial(t, self, version, []byte("trial B"))
		trialOnStart(p.bin, instKey)
		os.WriteFile(self, []byte(installed), 0o755)
		for i := 0; i < 3; i++ {
			trialReportOK()
		}
		if trialPending() || readFile(t, self) != "trial B" {
			t.Fatalf("штатный %q старше пробы, а проба не поставлена", installed)
		}
		for _, f := range []string{p.bin, p.mark} {
			if _, err := os.Stat(f); !os.IsNotExist(err) {
				t.Fatalf("%s остался", f)
			}
		}
		// следующий старт штатного (это уже проба B) ничего не откатывает
		trialOnStart(self, instKey)
		if v, _ := rejectedVersion.Load().(string); v != "" {
			t.Fatalf("принятая версия отвергнута: %q", v)
		}
	}
}

// Метка версии - межверсионный контракт: проба 2.76 читает ее из штатного файла 2.75. Метка
// есть в собранном бинаре (этот тестовый бинарь собран из тех же исходников).
func TestVersionMark(t *testing.T) {
	if versionMark != "\x00KERVAX_AGENT_VERSION="+version+"\x00" {
		t.Fatalf("формат метки: %q", versionMark)
	}
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if v := binaryVersion(exe); v != version {
		t.Fatalf("метка в бинаре: %q, нужна %q", v, version)
	}
	for in, want := range map[string]string{
		"x\x00KERVAX_AGENT_VERSION=2.80\x00":                                "2.80",
		"\x00KERVAX_AGENT_VERSION=bad\x00 \x00KERVAX_AGENT_VERSION=3.1\x00": "3.1",
		"\x00KERVAX_AGENT_VERSION=2.80":                                     "",
		"\x00KERVAX_AGENT_VERSION=\x00":                                     "",
		"\x00KERVAX_AGENT_VERSION=1." + strings.Repeat("1", 40) + "\x00":    "",
		"no mark at all": "",
	} {
		if got := markedVersion([]byte(in)); got != want {
			t.Fatalf("%q: %q, нужна %q", in, got, want)
		}
	}
	if binaryVersion(filepath.Join(t.TempDir(), "missing")) != "" {
		t.Fatal("версия у несуществующего файла")
	}
}

// Проба, которую панель больше не просит, убирается; заготовку под нынешнюю просьбу и маркер
// отказа для нее держим.
func TestDropStaleTrial(t *testing.T) {
	self, p := trialSetup(t)
	put := func(mark bool) {
		os.WriteFile(p.bin, []byte("trial"), 0o755)
		if mark {
			os.WriteFile(p.mark, []byte(`{"version":"9.99","tries":3}`), 0o644)
		}
	}
	exists := func(f string) bool { _, err := os.Stat(f); return err == nil }
	put(true)
	dropStaleTrialAt(self, "9.99")
	if !exists(p.bin) || !exists(p.mark) {
		t.Fatal("проба версии, которую панель просит, убрана")
	}
	dropStaleTrialAt(self, "")
	if exists(p.bin) || exists(p.mark) {
		t.Fatal("проба версии, которую панель больше не просит, осталась")
	}
	put(false)
	dropStaleTrialAt(self, "9.100")
	if !exists(p.bin) {
		t.Fatal("заготовка под нынешнюю просьбу убрана")
	}
	dropStaleTrialAt(self, version)
	if exists(p.bin) {
		t.Fatal("заготовка осталась, когда панель ничего не просит")
	}
	// процесс снятой пробы работает из пробного файла - его не трогаем
	put(true)
	dropStaleTrialAt(p.bin, "")
	if !exists(p.bin) || exists(p.mark) {
		t.Fatal("свой исполняемый файл убран или маркер остался")
	}
}

// Повтор пробы берет оставшийся бинарь, только если он ровно тот, что подписан.
func TestTrialBinaryChecksSHA(t *testing.T) {
	self, p := trialSetup(t)
	good := []byte("signed binary")
	art := artifact{SHA256: shaOf(string(good)), Size: len(good)}
	os.WriteFile(p.bin, []byte("forged binar!"), 0o755) // тот же размер
	if trialBinary(self, art) != nil {
		t.Fatal("взят бинарь с чужим sha256")
	}
	os.WriteFile(p.bin, good, 0o755)
	if !bytes.Equal(trialBinary(self, art), good) {
		t.Fatal("подписанный бинарь не взят")
	}
}

// Межверсионный контракт: старый бинарь пишет пробу, новый ее читает (selfupdate.go).
func TestTrialContract(t *testing.T) {
	if k := instanceKey("/etc/kervax-agent.conf"); k != "bc6316e69e24" {
		t.Fatalf("instanceKey изменился: %s", k)
	}
	p := trialPaths("/opt/kervax-agent/bin/kervax-agent", "bc6316e69e24")
	want := trialFiles{
		self: "/opt/kervax-agent/bin/kervax-agent",
		bin:  "/opt/kervax-agent/bin/.kervax-agent.trial-bc6316e69e24",
		mark: "/opt/kervax-agent/bin/.kervax-agent.trial-bc6316e69e24.json",
		prev: "/opt/kervax-agent/bin/kervax-agent.prev",
	}
	if p != want || stagedPath(p.bin) != p.self || stagedPath(p.self) != p.self {
		t.Fatalf("%+v", p)
	}
	b, _ := json.Marshal(trialMark{Version: "2.75", From: "2.74", Tries: 1, SelfSHA: "ab"})
	if string(b) != `{"version":"2.75","from":"2.74","tries":1,"self_sha":"ab"}` {
		t.Fatalf("формат маркера: %s", b)
	}
	if a, c := instanceKey("/etc/kervax-agent.conf"), instanceKey("/etc/kervax-agent-panel2.conf"); a == c {
		t.Fatal("у экземпляров один ключ")
	}
}

func notSynced() bool { return false }

func isSynced() bool { return true }

// Часы ноды отвели назад - свежесть манифеста считается от отметки этой загрузки плюс
// uptime, а не от системных часов. До 2.74 старый подписанный выпуск снова проходил.
func TestOTAClockIgnoresStepBack(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".kervax-agent.clock")
	real := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	if got := otaClock(path, "boot-a", 1000, real, notSynced); !got.Equal(real) {
		t.Fatalf("первая отметка: %v", got)
	}
	// прошел час, а часы отвели на 200 дней назад; агент мог и перезапуститься
	back := real.Add(time.Hour).Add(-200 * 24 * time.Hour)
	got := otaClock(path, "boot-a", 1000+3600, back, notSynced)
	if d := got.Sub(real.Add(time.Hour)); d < -time.Second || d > time.Second {
		t.Fatalf("сейчас %v, а нужно около %v", got, real.Add(time.Hour))
	}
	m := manifest{Version: "9.99", ReleasedAt: real.Add(-150 * 24 * time.Hour).Format(time.RFC3339)}
	if manifestFresh(m, got) == nil {
		t.Fatal("манифест 150-дневной давности принят после отвода часов")
	}
	// uptime меньше отмеченного (свой uptime у контейнера) - отметка не наша, ставим заново
	if got := otaClock(path, "boot-a", 10, back, notSynced); !got.Equal(back) {
		t.Fatalf("uptime меньше отметки: %v", got)
	}
	os.WriteFile(path, []byte("boot-a 1795000000 1000.00\n"), 0o644)
	// часы впереди отметки - верим им
	ahead := time.Unix(1795000000, 0).Add(48 * time.Hour)
	if got := otaClock(path, "boot-a", 1000+3600, ahead, notSynced); !got.Equal(ahead) {
		t.Fatalf("часы впереди: %v", got)
	}
	// новая загрузка - новая отметка
	if got := otaClock(path, "boot-b", 50, back, notSynced); !got.Equal(back) {
		t.Fatalf("новая загрузка: %v", got)
	}
	if !strings.HasPrefix(readFile(t, path), "boot-b ") {
		t.Fatal("отметка новой загрузки не записана")
	}
	// нет boot_id или uptime - только системные часы
	if got := otaClock(path, "", 0, back, notSynced); !got.Equal(back) {
		t.Fatalf("без boot_id: %v", got)
	}
}

// Часы на старте убежали вперед, потом демон NTP их поправил: отметка в будущем больше не держит
// обновления до перезагрузки. Без синхронизации (шаг helper'а) отметка стоит.
func TestOTAClockResetsWhenSynced(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".kervax-agent.clock")
	real := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	// RTC в localtime: на старте часы впереди UTC на 5 часов
	local := real.Add(5 * time.Hour)
	otaClock(path, "boot-a", 100, local, notSynced)
	if got := otaClock(path, "boot-a", 200, real, notSynced); got.Before(local) {
		t.Fatalf("без синхронизации отметка сдвинута: %v", got)
	}
	if got := otaClock(path, "boot-a", 300, real, isSynced); !got.Equal(real) {
		t.Fatalf("синхронизированные часы не приняты: %v", got)
	}
	if got := otaClock(path, "boot-a", 400, real.Add(100*time.Second), notSynced); !got.Equal(real.Add(100 * time.Second)) {
		t.Fatalf("отметка не переставлена: %v", got)
	}
}

// 2.75: синхронизированные часы отводят отметку назад не больше чем на otaMaxStepBack от самого
// позднего времени этой загрузки - и за один шаг, и шагами подряд. В 2.74 подложный NTP отводил
// ее на сколько угодно, и старый подписанный выпуск снова выглядел свежим.
func TestOTAClockStepBackLimit(t *testing.T) {
	real := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	step := func(back time.Duration) (time.Time, bool) {
		path := filepath.Join(t.TempDir(), ".kervax-agent.clock")
		otaClock(path, "boot-a", 100, real.Add(back), notSynced)
		return otaClockState(path, "boot-a", 100, real, isSynced)
	}
	if got, refused := step(6 * time.Hour); !got.Equal(real) || refused {
		t.Fatalf("откат ровно на 6 ч не принят: %v %v", got, refused)
	}
	if got, refused := step(6*time.Hour + time.Second); !got.Equal(real.Add(6*time.Hour+time.Second)) || !refused {
		t.Fatalf("откат на 6 ч с секундой принят: %v %v", got, refused)
	}
	if got, refused := step(200 * 24 * time.Hour); !got.Equal(real.Add(200*24*time.Hour)) || !refused {
		t.Fatalf("откат на 200 дней принят: %v %v", got, refused)
	}
	// шагами: 5 часов можно, еще 2 - уже 7 от самого позднего, нельзя
	path := filepath.Join(t.TempDir(), ".kervax-agent.clock")
	otaClock(path, "boot-a", 100, real.Add(5*time.Hour), notSynced)
	if got := otaClock(path, "boot-a", 100, real, isSynced); !got.Equal(real) {
		t.Fatalf("первый шаг: %v", got)
	}
	if got, refused := otaClockState(path, "boot-a", 100, real.Add(-2*time.Hour), isSynced); !got.Equal(real) || !refused {
		t.Fatalf("второй шаг назад принят: %v %v", got, refused)
	}
	// старт с отставшими часами (RTC сброшен), потом NTP вперед: самое позднее время запомнено,
	// и откат назад к старой дате не проходит
	path = filepath.Join(t.TempDir(), ".kervax-agent.clock")
	old := real.Add(-300 * 24 * time.Hour)
	otaClock(path, "boot-a", 100, old, notSynced)
	if got := otaClock(path, "boot-a", 200, real, notSynced); !got.Equal(real) {
		t.Fatalf("часы вперед: %v", got)
	}
	back := real.Add(100 * time.Second).Add(-200 * 24 * time.Hour)
	if got, refused := otaClockState(path, "boot-a", 300, back, isSynced); !got.Equal(real.Add(100*time.Second)) || !refused {
		t.Fatalf("откат после старта с отставшими часами: %v %v", got, refused)
	}
	// файл отметки 2.74 (три поля): самое позднее - сама отметка
	path = filepath.Join(t.TempDir(), ".kervax-agent.clock")
	os.WriteFile(path, []byte(fmt.Sprintf("boot-a %d 100.00\n", real.Add(7*time.Hour).Unix())), 0o644)
	if _, refused := otaClockState(path, "boot-a", 100, real, isSynced); !refused {
		t.Fatal("отметка 2.74 на 7 ч впереди сброшена синхронизированными часами")
	}
}

// selfUpdate судит о свежести по otaNow, а не по голым часам, и показывает оба времени.
func TestSelfUpdateChecksFreshnessByOTAClock(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	oldPub, oldWall, oldSync, oldPath := updatePubKeyB64, otaWall, clockSynced, otaClockPath
	defer func() { updatePubKeyB64, otaWall, clockSynced, otaClockPath = oldPub, oldWall, oldSync, oldPath }()
	updatePubKeyB64 = base64.StdEncoding.EncodeToString(pub)
	clock := filepath.Join(t.TempDir(), ".kervax-agent.clock")
	otaClockPath = func() string { return clock }
	clockSynced = func() bool { return false }
	real := time.Now()
	otaWall = func() time.Time { return real.Add(200 * 24 * time.Hour) } // часы ноды убежали
	man, _ := json.Marshal(map[string]any{"version": "9.99", "released_at": real.Add(-10 * 24 * time.Hour).UTC().Format(time.RFC3339),
		"artifacts": map[string]any{"amd64": map[string]any{"sha256": shaOf("x"), "size": 1}, "arm64": map[string]any{"sha256": shaOf("x"), "size": 1}}})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/agent/manifest":
			w.Write(man)
		case "/api/agent/manifest.sig":
			w.Write([]byte(base64.StdEncoding.EncodeToString(ed25519.Sign(priv, man))))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	err := selfUpdate(srv.URL, "9.99")
	if err == nil || !strings.Contains(err.Error(), "старше 120 дней по часам ноды") || isPermanent(err) {
		t.Fatalf("манифест судили не по часам OTA: %v", err)
	}
}

// signedPanel - панель с подписанным манифестом 9.99, выпущенным released; бинаря у нее нет
func signedPanel(t *testing.T, released time.Time) string {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	oldPub := updatePubKeyB64
	t.Cleanup(func() { updatePubKeyB64 = oldPub })
	updatePubKeyB64 = base64.StdEncoding.EncodeToString(pub)
	man, _ := json.Marshal(map[string]any{"version": "9.99", "released_at": released.UTC().Format(time.RFC3339),
		"artifacts": map[string]any{"amd64": map[string]any{"sha256": shaOf("x"), "size": 1}, "arm64": map[string]any{"sha256": shaOf("x"), "size": 1}}})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/agent/manifest":
			w.Write(man)
		case "/api/agent/manifest.sig":
			w.Write([]byte(base64.StdEncoding.EncodeToString(ed25519.Sign(priv, man))))
		default:
			http.Error(w, "no binary here", http.StatusForbidden)
		}
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// Часы на старте впереди (RTC в localtime, +5 ч), отметка загрузки ушла с ними; потом часы
// поправили. Отказ по свежести называет оба времени и причину - часы, а не старый выпуск, - и он
// временный: когда синхронизация сбросит отметку, следующая попытка проходит без перезагрузки. В
// 2.74 отказ был окончательным, версию не пробовали до перезапуска агента, а текст звал искать
// свежий выпуск. Откат на 200 дней синхронизированные часы не сбрасывают (2.75), отказ остается.
func TestOTAFreshRefusalByClockIsTemporary(t *testing.T) {
	if _, err := os.Stat("/proc/sys/kernel/random/boot_id"); err != nil {
		t.Skip("нет boot_id")
	}
	oldWall, oldSync, oldPath := otaWall, clockSynced, otaClockPath
	defer func() { otaWall, clockSynced, otaClockPath = oldWall, oldSync, oldPath }()
	clock := filepath.Join(t.TempDir(), ".kervax-agent.clock")
	otaClockPath = func() string { return clock }
	clockSynced = func() bool { return false }
	real := time.Now()
	// выпуск на грани: по верным часам свежий, по часам на 5 ч впереди - уже нет
	url := signedPanel(t, real.Add(-otaMaxAge+2*time.Hour))
	otaWall = func() time.Time { return real.Add(5 * time.Hour) }
	otaNow() // первая отметка загрузки - по часам в localtime
	otaWall = func() time.Time { return real }
	err := selfUpdate(url, "9.99")
	if err == nil || isPermanent(err) {
		t.Fatalf("отказ по отметке часов окончательный: %v", err)
	}
	for _, want := range []string{"по отметке часов этой загрузки", real.UTC().Format("2006-01-02 15:04"),
		real.Add(5 * time.Hour).UTC().Format("2006-01-02 15:04"), "синхронизации NTP"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("в отказе нет %q: %v", want, err)
		}
	}
	// демон NTP синхронизировал часы: отметка сброшена, свежесть пройдена - дальше скачивание
	clockSynced = func() bool { return true }
	if err := selfUpdate(url, "9.99"); err == nil || strings.Contains(err.Error(), "манифест") {
		t.Fatalf("после синхронизации отказ по свежести: %v", err)
	}
	// отметка на 200 дней впереди: синхронизированные часы ее не сбрасывают
	os.Remove(clock)
	otaWall = func() time.Time { return real.Add(200 * 24 * time.Hour) }
	clockSynced = func() bool { return false }
	otaNow()
	otaWall = func() time.Time { return real }
	clockSynced = func() bool { return true }
	err = selfUpdate(url, "9.99")
	if err == nil || isPermanent(err) || !strings.Contains(err.Error(), "отвели назад больше чем на 6 ч после загрузки") ||
		!strings.Contains(err.Error(), "после перезагрузки ноды или когда часы догонят отметку") {
		t.Fatalf("откат на 200 дней: %v", err)
	}
}

// Отказы по свежести: оба времени в тексте, укладываются в 300 символов панели вместе с версией,
// и ни один не окончательный - все зависят от часов. Выпуск из будущего - часы отстают.
func TestOTAFreshErrText(t *testing.T) {
	oldWall := otaWall
	defer func() { otaWall = oldWall }()
	otaStepBackRefused.Store(false)
	real := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	otaWall = func() time.Time { return real }
	cases := []struct {
		released, now time.Time
		want          []string
	}{
		{real.Add(-130 * 24 * time.Hour), real, []string{"старше 120 дней по часам ноды (2026-11-23 12:00 UTC)", "верные часы"}},
		{real.Add(-130 * 24 * time.Hour), real.Add(30 * 24 * time.Hour), []string{"по отметке часов этой загрузки (2026-12-23 12:00 UTC)", "часы ноды 2026-11-23 12:00"}},
		{real.Add(-100 * 24 * time.Hour), real.Add(30 * 24 * time.Hour), []string{"по отметке", "часы ноды 2026-11-23 12:00 UTC отвели назад больше чем на 6 ч"}},
		{real.Add(-120*24*time.Hour + time.Hour), real.Add(2 * time.Hour), []string{"по отметке часов этой загрузки (2026-11-23 14:00 UTC)", "синхронизации NTP"}},
		{real.Add(48 * time.Hour), real, []string{"в будущем по часам ноды (2026-11-23 12:00 UTC)", "отстают"}},
	}
	for _, c := range cases {
		m := manifest{Version: "2.123", ReleasedAt: c.released.Format(time.RFC3339)}
		ferr := manifestFresh(m, c.now)
		if ferr == nil {
			t.Fatalf("%v: манифест свежий", c.released)
		}
		err := otaFreshErr(m, ferr, c.now)
		if isPermanent(err) {
			t.Fatalf("отказ по часам окончательный: %v", err)
		}
		for _, w := range c.want {
			if !strings.Contains(err.Error(), w) {
				t.Fatalf("в отказе нет %q: %v", w, err)
			}
		}
		lastUpdateError.Store("2.123: " + err.Error())
		if txt := lastUpdateErrorText(); txt != "2.123: "+err.Error() {
			t.Fatalf("текст обрезан для панели (%d символов): %q", len([]rune("2.123: "+err.Error())), txt)
		}
	}
	lastUpdateError.Store("")
	// синхронизированные часы не пустили назад (шагами), хотя сейчас отметка впереди меньше 6 ч:
	// синхронизация ее уже не сбросит - текст про перезагрузку
	otaStepBackRefused.Store(true)
	m0 := manifest{Version: "2.123", ReleasedAt: real.Add(-120*24*time.Hour + time.Hour).Format(time.RFC3339)}
	if err := otaFreshErr(m0, manifestFresh(m0, real.Add(2*time.Hour)), real.Add(2*time.Hour)); isPermanent(err) ||
		!strings.Contains(err.Error(), "отвели назад больше чем на 6 ч") {
		t.Fatalf("отказ после отвергнутого шага назад: %v", err)
	}
	otaStepBackRefused.Store(false)
	// нет даты выпуска - от часов не зависит, окончательный
	m := manifest{Version: "2.123"}
	if err := otaFreshErr(m, manifestFresh(m, real), real); !isPermanent(err) {
		t.Fatalf("манифест без даты: %v", err)
	}
	// длинный текст режется по символам, а не посреди буквы
	lastUpdateError.Store(strings.Repeat("я", 400))
	if txt := lastUpdateErrorText(); len([]rune(txt)) != 300 || !utf8.ValidString(txt) {
		t.Fatalf("срез: %d символов, utf8 %v", len([]rune(txt)), utf8.ValidString(txt))
	}
	lastUpdateError.Store("")
}

// timesync-setup 0.4: HTTP-фолбэк не уводит часы больше чем на 6 часов назад от последнего
// виденного времени, сколько бы запросов ни пришло. 0.3 отводил по 6 часов за запрос.
func TestTimesyncHelperNoStepwiseRollback(t *testing.T) {
	src, err := os.ReadFile("timesync-setup.sh")
	if err != nil {
		t.Fatal(err)
	}
	_, body, ok := strings.Cut(string(src), "cat > \"$HELPER.new\" <<'HELPER_EOF'\n")
	if !ok {
		t.Fatal("тело helper'а не найдено")
	}
	body, _, ok = strings.Cut(body, "\ncase \"${1:-}\" in\n")
	if !ok {
		t.Fatal("разбор аргументов helper'а не найден")
	}
	const test = `
dir="$1"
FLOOR="$dir/tsync-floor"
CONF="$dir/kervax-agent.conf"
printf 'url=https://panel.example\n' > "$CONF"
NOW=1795000000
systemctl() { [ "$1" = is-active ] && echo inactive; return 0; }
timedatectl() { [ "$1" = show ] && echo no; return 0; }
chronyc() { return 1; }
sleep() { :; }
grep() { if [ "$1" = -lxF ]; then echo "$CONF"; else command grep "$@"; fi; }
curl() { printf 'HTTP/1.1 200 OK\r\nDate: %s\r\n\r\n' "$(command date -u -d "@$PANEL" '+%a, %d %b %Y %H:%M:%S GMT')"; }
date() {
  case "$1" in
    +%s) echo "$NOW" ;;
    -s) NOW="${2#@}" ;;
    -d) command date "$@" ;;
    *) command date -d "@$NOW" "$@" ;;
  esac
}
step() { # $1 - на сколько часов панель позади ноды; $2 - ждем ли шаг
  PANEL=$((NOW - $1 * 3600))
  if cmd_sync https://panel.example >"$dir/out" 2>&1; then r=yes; else r=no; fi
  out=$(cat "$dir/out")
  [ "$r" = "$2" ] || { echo "panel $1h behind: stepped=$r, want $2 (now $NOW, floor $(cat "$FLOOR" 2>/dev/null))"; exit 1; }
}
start=$NOW
step 5 yes                # обычная поправка на 5 часов назад
[ "$NOW" -eq $((start - 5 * 3600)) ] || { echo "clock not stepped"; exit 1; }
step 5 no                 # еще 5 часов назад - уже 10 от виденного: отказ
case "$out" in *"$FLOOR"*) ;; *) echo "the refusal does not name $FLOOR: $out"; exit 1 ;; esac
step 1 yes                # в пределах 6 часов от виденного - можно
step 3 no
NOW=$((start + 100)); step 0 yes   # вперед и к настоящему времени - всегда можно
# отказ отметку не двигает: часы поправили руками на 8 часов назад от нее, фолбэк ведет еще на 1
# отказ отметку не двигает: часы ушли на 10 часов вперед, фолбэк отказал; поправили руками -
# фолбэк снова работает (0.4 до правки писал отметку до запроса и отказывал дальше)
mark=$(cat "$FLOOR")
NOW=$((mark + 10 * 3600)); step 10 no
[ "$(cat "$FLOOR")" = "$mark" ] || { echo "a refused step moved the mark"; exit 1; }
NOW=$((mark + 60)); step 1 yes
`
	cmd := exec.Command("bash", "-c", body+test, "timesync-test", t.TempDir())
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("helper: %v\n%s", err, bytes.TrimSpace(out))
	}
}

// adjtimex (вне @system-service) зовем только в редком случае: отметка впереди часов. Под
// SystemCallFilter без SystemCallErrorNumber вызов на старте убил бы агента SIGSYS.
func TestOTAClockCallsAdjtimexOnlyWhenMarkAhead(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".kervax-agent.clock")
	calls := 0
	synced := func() bool { calls++; return true }
	real := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	// нет отметки; отметка позади часов; впереди меньше минуты; новая загрузка
	otaClock(path, "boot-a", 100, real, synced)
	otaClock(path, "boot-a", 200, real.Add(100*time.Second), synced)
	otaClock(path, "boot-a", 300, real.Add(170*time.Second), synced)
	otaClock(path, "boot-b", 10, real, synced)
	if calls != 0 {
		t.Fatalf("adjtimex вызван %d раз на обычном пути", calls)
	}
	otaClock(path, "boot-b", 20, real.Add(-time.Hour), synced)
	if calls != 1 {
		t.Fatalf("adjtimex не вызван, когда отметка впереди часов: %d", calls)
	}
	// старт агента: otaNow берет clockSynced лениво
	old, oldPath := clockSynced, otaClockPath
	defer func() { clockSynced, otaClockPath = old, oldPath }()
	clockSynced = func() bool { t.Fatal("adjtimex на старте"); return false }
	otaClockPath = func() string { return filepath.Join(t.TempDir(), ".kervax-agent.clock") }
	otaNow()
}

// Блокировка коммита - страховка: файл не открыть или ФС без flock - коммитим без нее;
// прочий отказ flock откладывает коммит.
func TestTrialCommitLockFallbacks(t *testing.T) {
	oldFlock := flockFn
	defer func() { flockFn = oldFlock }()
	commit := func(t *testing.T, prep func(self string)) (string, bool) {
		self, p := trialSetup(t)
		runTrial(t, self, version, []byte("new binary"))
		trialOnStart(p.bin, instKey)
		prep(self)
		for i := 0; i < 3; i++ {
			trialReportOK()
		}
		return readFile(t, self), trialPending()
	}
	flockFn = oldFlock
	got, pending := commit(t, func(self string) {
		// файл блокировки не открыть: ссылка в несуществующий каталог
		os.Symlink("/nonexistent-kervax/lock", filepath.Join(filepath.Dir(self), ".kervax-agent.lock"))
	})
	if got != "new binary" || pending {
		t.Fatal("без файла блокировки коммит не прошел")
	}
	flockFn = func(int, int) error { return syscall.ENOLCK }
	if got, pending := commit(t, func(string) {}); got != "new binary" || pending {
		t.Fatal("на ФС без flock коммит не прошел")
	}
	flockFn = func(int, int) error { return syscall.EIO }
	if got, pending := commit(t, func(string) {}); got != "old binary" || !pending {
		t.Fatal("при сбое flock коммит не отложен")
	}
}

// exec не случился - попытки не было: счетчик в маркере возвращается.
func TestExecFailureRollsBackTry(t *testing.T) {
	self, p := trialSetup(t)
	execFn = func(string, []string, []string) error { return errors.New("ENOEXEC") }
	if err := startTrial(self, "9.99", []byte("new binary")); err == nil {
		t.Fatal("ошибка exec не вернулась")
	}
	if _, err := os.Stat(p.mark); !os.IsNotExist(err) {
		t.Fatal("маркер первой попытки остался после неудачного exec")
	}
	os.WriteFile(p.mark, []byte(`{"version":"9.99","tries":1}`), 0o644)
	startTrial(self, "9.99", []byte("new binary"))
	var m trialMark
	if json.Unmarshal([]byte(readFile(t, p.mark)), &m) != nil || m.Tries != 1 {
		t.Fatalf("после неудачного exec попыток %d, нужна 1", m.Tries)
	}
}
