package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// ---- Агент 2.74: самообновление через пробный запуск, слив команд перед exec, часы для OTA ----
//
// До 2.74 новый бинарь писался поверх штатного без fsync и запускался exec'ом сразу, посреди
// выполняющихся команд панели. После сбоя питания rename мог дойти до диска раньше данных, а
// бинарь, который падает на старте, systemd поднимал снова и снова - вернуть прежний было нечем.
// Команды, взятые у панели (при выдаче панель помечает их running), гибли вместе со старым
// процессом, и через 90 с панель писала «агент не ответил».
//
// Теперь новый бинарь пишется рядом под своим именем (fsync файла и каталога), и exec идет В
// НЕГО, а штатный файл, который запускает юнит, остается прежним. Новый процесс ставит себя на
// место штатного, только когда панель приняла trialCommitReports его отчетов и он прожил
// trialCommitAge (trialReportOK). Не стартовал, упал, завис или перезапустился раньше - systemd
// (Restart=always, WatchdogSec) поднимает штатный бинарь, то есть прежнюю версию. Юнит не
// меняется, поэтому так работает на всех нодах. Прежняя версия видит маркер несостоявшейся пробы
// и считает неудачу (trialOnStart); после trialMax неудач подряд эту версию больше не ставит.
// Предыдущий бинарь сохраняется как <имя>.prev - для ручного отката уже принятой версии. PID при
// exec тот же, поэтому для Type=notify это тот же сервис: новый процесс снова шлет READY=1,
// вотчдог кормит основной цикл и во время слива.
//
// Анти-откат версии не задет: проба не меняет const version старого бинаря, и после неудачи он
// сравнивает следующий манифест со своей версией, как раньше. Состояния «самая новая версия»
// на диске нет, так что ни неудачная проба, ни ручной возврат .prev следующее обновление не ломают.
//
// Межверсионный контракт: старый бинарь пишет пробу и маркер, новый их читает и коммитит,
// следующий старый снова читает маркер. Поэтому instanceKey (sha256 абсолютного пути конфига,
// первые 12 hex), имена файлов (trialPaths) и поля trialMark менять нельзя без совместимости -
// их держит TestTrialContract.

// trialMax - столько пробных запусков одной версии подряд может сорваться, дальше она отвергнута
const trialMax = 3

// Проба принята, когда панель приняла столько ее отчетов и процесс прожил столько времени.
// Паника в фоновом цикле (пакеты, порты, обновления ОС) или в обработчике команды случается
// не в первую секунду; после коммита откатывать уже некуда.
var (
	trialCommitReports = 3
	trialCommitAge     = 5 * time.Minute
)

// agentStart - запуск этого процесса (после exec - запуск пробы); с монотонными часами
var agentStart = time.Now()

// drainMax - сколько перед exec ждать команды, уже взятые у панели. Самое долгое ожидание
// helper'а - 15 минут (disk_fix run), сверху - отправка результата.
var drainMax = 16 * time.Minute

var drainTick = 200 * time.Millisecond

// execFn - замена процесса; тесты подставляют свою
var execFn = syscall.Exec

// taskGate считает работу, которую exec оборвал бы с потерей результата: опрос очереди команд,
// отчет основного цикла и каждую взятую у панели команду до отправки ее результата.
type taskGate struct {
	mu       sync.Mutex
	n        int
	draining bool // идет слив: новые опросы очереди не начинаем, в отчете просим команд не давать
	sealed   bool // слив закончен, сейчас exec: и отчет не шлем
}

var tasks taskGate

// poll - начать опрос очереди команд. Во время слива - нет: команды останутся в очереди
// панели, и их заберет новый процесс.
func (g *taskGate) poll() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.draining {
		return false
	}
	g.n++
	return true
}

// report - отчет основного цикла. Идет и во время слива, чтобы панель не сочла ноду
// пропавшей; отчет несет draining, и панель 2.74+ команд в ответе не дает. Старая дает - они
// уже взяты, выполняются, и слив их дожидается.
func (g *taskGate) report() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.sealed {
		return false
	}
	g.n++
	return true
}

func (g *taskGate) isDraining() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.draining
}

// spawn - выполнить взятую у панели задачу в своей горутине. Зовется, пока держится poll или
// report, поэтому счетчик не падает до нуля между выдачей команды и ее запуском.
func (g *taskGate) spawn(f func()) {
	g.mu.Lock()
	g.n++
	g.mu.Unlock()
	go func() {
		defer g.done()
		f()
	}()
}

func (g *taskGate) done() {
	g.mu.Lock()
	g.n--
	g.mu.Unlock()
}

// drain - перестать брать новое и дождаться начатого, не дольше max. Возвращает, сколько
// задач так и не закончилось.
func (g *taskGate) drain(max time.Duration) int {
	g.mu.Lock()
	g.draining = true
	g.mu.Unlock()
	deadline := time.Now().Add(max)
	for {
		g.mu.Lock()
		if g.n <= 0 || !time.Now().Before(deadline) {
			g.sealed = true
			left := g.n
			g.mu.Unlock()
			return left
		}
		g.mu.Unlock()
		time.Sleep(drainTick)
	}
}

// resume - exec не случился: работаем дальше как обычно
func (g *taskGate) resume() {
	g.mu.Lock()
	g.draining, g.sealed = false, false
	g.mu.Unlock()
}

// reportOnce - отчет основного цикла под tasks. После печати слива отчет не идет: exec вот-вот,
// а в ответе на отчет старая панель выдала бы новые команды. ok=false - отчета не было, и
// tasks.done() звать не нужно.
func reportOnce(url, token string, prev sample) (r report, cur sample, cfg config, err error, ok bool) {
	if !tasks.report() {
		return report{}, prev, config{}, nil, false
	}
	r, cur, cfg, err = collectAndSend(url, token, prev)
	return r, cur, cfg, err, true
}

// instKey - ключ экземпляра агента. Бинарь у kervax-agent и kervax-agent@<имя> общий, а
// конфиги разные: пробы разных экземпляров не должны делить файлы.
var instKey string

func instanceKey(cfgPath string) string {
	if abs, err := filepath.Abs(cfgPath); err == nil {
		cfgPath = abs
	}
	sum := sha256.Sum256([]byte(cfgPath))
	return hex.EncodeToString(sum[:6])
}

// пробный бинарь: .<имя штатного>.trial-<ключ экземпляра>
var trialName = regexp.MustCompile(`^\.(.+)\.trial-([0-9a-f]{12})$`)

type trialFiles struct {
	self string // штатный бинарь, его запускает юнит
	bin  string // пробный
	mark string // маркер пробы
	prev string // копия предыдущего бинаря
}

func trialPaths(self, key string) trialFiles {
	bin := filepath.Join(filepath.Dir(self), "."+filepath.Base(self)+".trial-"+key)
	return trialFiles{self: self, bin: bin, mark: bin + ".json", prev: self + ".prev"}
}

// stagedPath - штатный бинарь для этого процесса: пробный знает его по своему имени
func stagedPath(self string) string {
	if m := trialName.FindStringSubmatch(filepath.Base(self)); m != nil {
		return filepath.Join(filepath.Dir(self), m[1])
	}
	return self
}

type trialMark struct {
	Version string `json:"version"`  // что пробуем
	From    string `json:"from"`     // с какой версии
	Tries   int    `json:"tries"`    // сколько раз запускали подряд
	SelfSHA string `json:"self_sha"` // sha256 штатного бинаря при запуске пробы
}

func readTrialMark(path string) (trialMark, bool) {
	var m trialMark
	b, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(b, &m) != nil || m.Version == "" {
		return trialMark{}, false
	}
	return m, true
}

var (
	trialMu      sync.Mutex
	trialNow     *trialFiles // этот процесс - пробный и еще не стал штатным
	trialReports int         // сколько его отчетов панель уже приняла
	trialFrom    string      // с какой версии проба (из маркера): к ней вернет systemd, если проба сорвется
)

func trialPending() bool {
	trialMu.Lock()
	defer trialMu.Unlock()
	return trialNow != nil
}

// trialState - для отчета: идет ли проба и с какой версии
func trialState() (bool, string) {
	trialMu.Lock()
	defer trialMu.Unlock()
	if trialNow == nil {
		return false, ""
	}
	return true, trialFrom
}

func setTrial(p *trialFiles) {
	trialMu.Lock()
	trialNow, trialReports = p, 0
	trialMu.Unlock()
}

func selfExe() (string, error) {
	self, err := os.Executable()
	if err != nil {
		return "", err
	}
	if resolved, e := filepath.EvalSymlinks(self); e == nil {
		self = resolved
	}
	return self, nil
}

func fileSHA(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return ""
	}
	return hex.EncodeToString(h.Sum(nil))
}

// syncDir - rename виден после сбоя питания, только когда записан сам каталог
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

// writeSync - файл целиком через временный рядом: fsync данных, rename, fsync каталога
func writeSync(path string, data []byte, perm os.FileMode) error {
	tmp := fmt.Sprintf("%s.tmp-%d", path, os.Getpid())
	os.Remove(tmp)
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, perm)
	if err != nil {
		return err
	}
	_, err = f.Write(data)
	if err == nil {
		err = f.Chmod(perm) // umask не должен отнять права
	}
	if err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp, path)
	}
	if err != nil {
		os.Remove(tmp)
		return err
	}
	return syncDir(filepath.Dir(path))
}

// savePrev - копия ВЫПОЛНЯЮЩЕГОСЯ бинаря: /proc/self/exe - это он, даже если штатный файл уже
// заменил другой экземпляр агента
func savePrev(prev string) error {
	src, err := os.Open("/proc/self/exe")
	if err != nil {
		self, e := selfExe()
		if e != nil {
			return e
		}
		if src, err = os.Open(self); err != nil {
			return err
		}
	}
	defer src.Close()
	data, err := io.ReadAll(io.LimitReader(src, 256<<20))
	if err != nil {
		return err
	}
	return writeSync(prev, data, 0o755)
}

// trialBinary - проверенный бинарь, оставшийся от сорвавшейся пробы этой же версии: повтор
// не качает его заново (за DPI это десятки минут)
func trialBinary(self string, art artifact) []byte {
	b, err := os.ReadFile(trialPaths(self, instKey).bin)
	if err != nil || len(b) != art.Size {
		return nil
	}
	sum := sha256.Sum256(b)
	if hex.EncodeToString(sum[:]) != art.SHA256 {
		return nil
	}
	return b
}

// startTrial - бинарь newVer уже проверен по подписанному манифесту: ставим рядом, сохраняем
// текущий как .prev, сливаем команды и перезапускаемся в новый. При успехе не возвращается.
func startTrial(self, newVer string, bin []byte) error {
	p := trialPaths(self, instKey)
	if err := writeSync(p.bin, bin, 0o755); err != nil {
		return fmt.Errorf("запись %s: %w (нужны права на каталог агента)", p.bin, err)
	}
	if err := savePrev(p.prev); err != nil {
		return fmt.Errorf("копия текущего бинаря в %s: %w", p.prev, err)
	}
	return execTrial(p, newVer)
}

// execTrial - слив, маркер, exec. Маркер пишется только после слива: перезапуск во время слива
// (до 16 минут: перезагрузка, вотчдог) - не попытка пробы, и считать его неудачей нельзя.
func execTrial(p trialFiles, newVer string) error {
	if left := tasks.drain(drainMax); left > 0 {
		fmt.Fprintf(os.Stderr, "kervax-agent: за %s не закончилось команд: %d - перезапуск без них\n", drainMax, left)
	}
	m, _ := readTrialMark(p.mark)
	if m.Version != newVer {
		m = trialMark{Version: newVer}
	}
	m.From = version
	m.Tries++
	m.SelfSHA = fileSHA(p.self)
	mb, _ := json.Marshal(m)
	if err := writeSync(p.mark, mb, 0o644); err != nil {
		tasks.resume()
		return fmt.Errorf("маркер пробы %s: %w", p.mark, err)
	}
	fmt.Printf("kervax-agent: обновлён %s → %s, пробный запуск %d из %d\n", version, newVer, m.Tries, trialMax)
	err := execFn(p.bin, os.Args, os.Environ()) // тот же PID, systemd доволен
	// exec не случился - и попытки не было
	if m.Tries--; m.Tries > 0 {
		mb, _ = json.Marshal(m)
		_ = writeSync(p.mark, mb, 0o644)
	} else {
		os.Remove(p.mark)
	}
	tasks.resume()
	return fmt.Errorf("запуск %s: %w", p.bin, err)
}

// trialOnStart - разбор пробы при старте. Запущены из пробного бинаря - запоминаем: штатный
// заменим, когда проба себя покажет. Запущены из штатного, а в маркере версия новее нашей -
// проба сорвалась (упала, вотчдог, перезагрузка): считаем неудачу и остаемся как есть.
func trialOnStart(self, key string) {
	if m := trialName.FindStringSubmatch(filepath.Base(self)); m != nil {
		if m[2] == key {
			p := trialPaths(filepath.Join(filepath.Dir(self), m[1]), key)
			from := ""
			if mk, ok := readTrialMark(p.mark); ok && len(mk.From) <= 32 && versionRe.MatchString(mk.From) {
				from = mk.From
			}
			setTrial(&p)
			trialMu.Lock()
			trialFrom = from
			trialMu.Unlock()
			fmt.Printf("kervax-agent %s: пробный запуск, %s заменим после %d принятых отчетов и %s работы\n",
				version, p.self, trialCommitReports, trialCommitAge)
		}
		return
	}
	p := trialPaths(self, key)
	m, ok := readTrialMark(p.mark)
	if !ok {
		return
	}
	if !versionNewer(m.Version, version) {
		// проба принята (или эту версию поставили руками): ни маркер, ни пробный бинарь не нужны
		os.Remove(p.bin)
		os.Remove(p.mark)
		return
	}
	msg := fmt.Sprintf("версия %s не продержалась пробу (попытка %d из %d), работаем на %s",
		m.Version, m.Tries, trialMax, version)
	if m.Tries >= trialMax {
		rejectedVersion.Store(m.Version)
		os.Remove(p.bin)
		msg += " - больше ее не ставим"
	} else {
		updateRetryAt.Store(time.Now().Add(updateRetryPause))
	}
	fmt.Fprintln(os.Stderr, "kervax-agent:", msg)
	lastUpdateError.Store(m.Version + ": " + msg)
}

// versionMark - версия в файле бинаря: при коммите пробы ее читают из штатного файла, не
// запуская его (binaryVersion). Формат - часть межверсионного контракта (TestVersionMark).
var versionMark = "\x00KERVAX_AGENT_VERSION=" + version + "\x00"

// binaryVersion - версия из метки в файле бинаря; "" - файла нет или метки нет (агент до 2.75)
func binaryVersion(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return markedVersion(b)
}

// markedVersion ищет метку по префиксу, вырезанному из своей же метки: отдельной строки с
// префиксом в бинаре нет, и найти можно только настоящую метку
func markedVersion(b []byte) string {
	key := []byte(versionMark[:strings.IndexByte(versionMark, '=')+1])
	for {
		i := bytes.Index(b, key)
		if i < 0 {
			return ""
		}
		b = b[i+len(key):]
		if end := bytes.IndexByte(b, 0); end > 0 && end <= 32 && versionRe.Match(b[:end]) {
			return string(b[:end])
		}
	}
}

var versionRe = regexp.MustCompile(`^\d+(\.\d+)*$`)

var flockFn = syscall.Flock // тесты подставляют отказ

// lockDir - взаимоисключение экземпляров агента с общим бинарем на время коммита. Блокировка -
// страховка, а не условие: файл не открыть (чужой, root-owned) или ФС без flock (ENOLCK,
// EOPNOTSUPP, ENOSYS) - коммитим без нее, с записью в журнал. Ошибка - только прочие отказы flock.
func lockDir(dir string) (func(), error) {
	path := filepath.Join(dir, ".kervax-agent.lock")
	f, err := os.OpenFile(path, os.O_RDONLY|os.O_CREATE, 0o644)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: блокировка %s: %v - коммит без нее\n", path, err)
		return func() {}, nil
	}
	if err := flockFn(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		if errors.Is(err, syscall.ENOLCK) || errors.Is(err, syscall.EOPNOTSUPP) || errors.Is(err, syscall.ENOSYS) {
			fmt.Fprintf(os.Stderr, "kervax-agent: блокировка %s: %v - коммит без нее\n", path, err)
			return func() {}, nil
		}
		return nil, err
	}
	return func() { f.Close() }, nil
}

// trialReportOK - панель приняла отчет. Пробный процесс, который продержался
// trialCommitReports отчетов и trialCommitAge, становится штатным.
func trialReportOK() {
	trialMu.Lock()
	p := trialNow
	if p != nil {
		trialReports++
	}
	n := trialReports
	trialMu.Unlock()
	if p == nil || n < trialCommitReports || time.Since(agentStart) < trialCommitAge {
		return
	}
	dir := filepath.Dir(p.self)
	unlock, err := lockDir(dir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: блокировка %s: %v - коммит пробы позже\n", dir, err)
		return
	}
	defer unlock()
	m, ok := readTrialMark(p.mark)
	if !ok || m.Version != version || m.SelfSHA == "" || fileSHA(p.self) != m.SelfSHA {
		// Штатный за время пробы сменил другой экземпляр агента (общий бинарь) или маркер
		// пропал. Побеждает новейшая версия, а не первый коммит (2.75): в 2.74 проба 2.80 тихо
		// снималась, если другой экземпляр успел поставить 2.79, процесс работал 2.80 из
		// пробного файла, панель снимала цель по его отчету, а после рестарта нода молча
		// становилась 2.79. Штатный не старше нас - поверх не пишем (анти-откат), пробу снимаем
		// без неудачи: после рестарта будет версия не ниже нашей. Старше или версии в нем нет
		// (бинари до 2.75 метку не несут, они старше этого кода) - ставим свою поверх.
		inst := binaryVersion(p.self)
		if inst != "" && !versionNewer(version, inst) {
			os.Remove(p.mark)
			setTrial(nil)
			fmt.Fprintf(os.Stderr, "kervax-agent %s: %s сменился во время пробы на %s - не заменяю его, проба снята\n",
				version, p.self, inst)
			return
		}
		if inst == "" {
			inst = "без метки версии"
		}
		fmt.Fprintf(os.Stderr, "kervax-agent %s: %s сменился во время пробы, там версия старше (%s) - ставлю свою\n",
			version, p.self, inst)
	}
	if err := os.Rename(p.bin, p.self); err != nil {
		// повторим со следующим отчетом; до тех пор юнит при рестарте поднимет прежнюю версию
		fmt.Fprintf(os.Stderr, "kervax-agent: не заменил %s новой версией: %v\n", p.self, err)
		lastUpdateError.Store(fmt.Sprintf("%s: не заменил %s: %v", version, p.self, err))
		return
	}
	// rename прошел - проба принята, что бы ни случилось дальше
	setTrial(nil)
	os.Remove(p.mark)
	if err := syncDir(dir); err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: fsync %s: %v\n", dir, err)
	}
	fmt.Printf("kervax-agent %s: проба принята, %s заменен (прежний - %s)\n", version, p.self, p.prev)
}

// dropStaleTrial - панель больше не просит версию пробы: пробный бинарь и маркер не нужны.
// want - версия, которую панель просит сейчас ("" - ничего не просит).
func dropStaleTrial(want string) {
	if trialPending() {
		return
	}
	if self, err := selfExe(); err == nil {
		dropStaleTrialAt(self, want)
	}
}

func dropStaleTrialAt(self, want string) {
	p := trialPaths(stagedPath(self), instKey)
	if want == version {
		want = ""
	}
	m, ok := readTrialMark(p.mark)
	if ok && m.Version == want {
		return // ждем повтора или держим отказ
	}
	if !ok && want != "" {
		return // бинарь без маркера - заготовка под нынешнюю просьбу (trialBinary сверит sha)
	}
	if p.bin != self { // свой исполняемый файл не трогаем: процесс снятой пробы работает из него
		os.Remove(p.bin)
	}
	os.Remove(p.mark)
}

// ---- часы для свежести манифеста ----
//
// otaMaxAge считается по часам ноды, а их можно отвести назад: helper timesync шагает к часам
// панели (до timesync-setup 0.4 - до 6 часов за запрос, но запросов сколько угодно подряд), и
// давно выпущенный подписанный манифест снова выглядел свежим. Поэтому «сейчас» для этой
// проверки не меньше отметки, которую агент поставил первым запуском в текущую загрузку
// системы, плюс прошедший с нее uptime. Шаг часов назад окно не раздвигает, даже если агента
// перезапустят; после перезагрузки отметка новая.
//
// Часы, которые ядро считает синхронизированными (демон NTP сбросил STA_UNSYNC), отметку
// переставляют назад, но не дальше otaMaxStepBack от самого позднего времени, виденного в эту
// загрузку (2.75). В 2.74 синхронизированным часам верили целиком, а NTP без аутентификации
// подделывается: подложный сервер (или chronyc makestep по просьбе панели) отводил часы на
// сколько угодно, отметка шла за ними, и старый подписанный выпуск снова выглядел свежим. Шаг
// helper'а (date -s) STA_UNSYNC выставляет, и отметку такой шаг не сдвигает вовсе.

// otaMaxStepBack - на сколько синхронизированные часы могут отвести отметку назад за одну
// загрузку; аналог MAX_STEP=21600 в helper'е timesync-setup. 6 часов: часы в RTC по местному
// времени (localtime) на старте для наших поясов впереди UTC на 3-5 часов, и такая нода после
// синхронизации NTP должна обновляться без перезагрузки. Откат больше - отметка стоит, отказ
// временный: обновление пойдет после перезагрузки ноды или когда часы догонят отметку.
const otaMaxStepBack = 6 * time.Hour

// otaStepBackRefused - последний otaNow не пустил синхронизированные часы назад дальше
// otaMaxStepBack: для текста отказа
var otaStepBackRefused atomic.Bool

var otaWall = time.Now // тесты подставляют свои часы

// clockSynced - ядро считает часы синхронизированными (adjtimex только читаем, прав не нужно)
var clockSynced = func() bool {
	var tx syscall.Timex
	state, err := syscall.Adjtimex(&tx)
	const timeError, staUnsync = 5, 0x40
	return err == nil && state != timeError && tx.Status&staUnsync == 0
}

var otaClockPath = func() string {
	self, err := selfExe()
	if err != nil {
		return ""
	}
	return filepath.Join(filepath.Dir(stagedPath(self)), ".kervax-agent.clock")
}

func otaNow() time.Time {
	boot, _ := os.ReadFile("/proc/sys/kernel/random/boot_id")
	up := 0.0
	if b, err := os.ReadFile("/proc/uptime"); err == nil {
		if f := strings.Fields(string(b)); len(f) > 0 {
			up, _ = strconv.ParseFloat(f[0], 64)
		}
	}
	t, refused := otaClockState(otaClockPath(), strings.TrimSpace(string(boot)), up, otaWall(), clockSynced)
	otaStepBackRefused.Store(refused)
	return t
}

func otaClock(path, boot string, up float64, now time.Time, synced func() bool) time.Time {
	t, _ := otaClockState(path, boot, up, now, synced)
	return t
}

// otaClockState - max(now, отметка этой загрузки + прошедший uptime). Файл: "boot wall up" -
// отметка, и с 2.75 еще "hiwall hiup" - самое позднее время, виденное в эту загрузку (у файла
// 2.74 оно равно отметке). Нет отметки этой загрузки - ставим по now. Часы ушли вперед дальше
// самого позднего - запоминаем (иначе после старта с отставшими часами отметка ничего не
// держала бы). Отметка впереди часов, а часы синхронизированы: ставим ее по now, если это не
// дальше otaMaxStepBack от самого позднего; иначе отметка стоит, refused.
func otaClockState(path, boot string, up float64, now time.Time, synced func() bool) (time.Time, bool) {
	if path == "" || boot == "" || up <= 0 {
		return now, false
	}
	at := func(wall int64, was float64) time.Time {
		return time.Unix(wall, 0).Add(time.Duration((up - was) * float64(time.Second)))
	}
	save := func(wall int64, was float64, hiWall int64, hiWas float64) {
		_ = writeSync(path, []byte(fmt.Sprintf("%s %d %.2f %d %.2f\n", boot, wall, was, hiWall, hiWas)), 0o644)
	}
	if b, err := os.ReadFile(path); err == nil {
		f := strings.Fields(string(b))
		if (len(f) == 3 || len(f) == 5) && f[0] == boot {
			wall, e1 := strconv.ParseInt(f[1], 10, 64)
			was, e2 := strconv.ParseFloat(f[2], 64)
			hiWall, hiWas, e3, e4 := wall, was, error(nil), error(nil)
			if len(f) == 5 {
				hiWall, e3 = strconv.ParseInt(f[3], 10, 64)
				hiWas, e4 = strconv.ParseFloat(f[4], 64)
			}
			if e1 == nil && e2 == nil && e3 == nil && e4 == nil && was <= up && hiWas <= up {
				floor, hi := at(wall, was), at(hiWall, hiWas)
				if !floor.After(now) {
					if now.Sub(hi) > time.Minute {
						save(now.Unix(), up, now.Unix(), up)
					}
					return now, false
				}
				// adjtimex - только здесь, в редком случае: вне @system-service, и под
				// SystemCallFilter без SystemCallErrorNumber вызов на старте убил бы агента
				if floor.Sub(now) < time.Minute || !synced() {
					return floor, false
				}
				if hi.Sub(now) > otaMaxStepBack {
					return floor, true
				}
				save(now.Unix(), up, hiWall, hiWas)
				return now, false
			}
		}
	}
	save(now.Unix(), up, now.Unix(), up)
	return now, false
}

// otaFreshErr - отказ по свежести, которым судили по now (otaNow). Отказ временный: он зависит от
// часов, а их поправят - синхронизация сбросит отметку, и следующая попытка пройдет. В 2.74 отказ
// был окончательным: версию больше не пробовали до перезапуска агента, даже когда часы уже
// исправили, а текст «старше 120 дней (нужен свежий выпуск)» звал искать новый выпуск, когда
// дело было в часах. Поэтому в тексте - время, по которому судили, и часы ноды. Длина: панель
// принимает 300 символов вместе с версией впереди (TestOTAFreshErrText).
func otaFreshErr(m manifest, err error, now time.Time) error {
	const tf = "2006-01-02 15:04"
	wall := otaWall()
	t, perr := time.Parse(time.RFC3339Nano, m.ReleasedAt)
	switch {
	case perr != nil:
		return permanent(err) // даты нет - от часов не зависит
	case t.After(now):
		return fmt.Errorf("манифест %s выпущен %s, в будущем по часам ноды (%s UTC) - отказ: часы ноды отстают? Повторим позже",
			m.Version, t.UTC().Format(tf), wall.UTC().Format(tf))
	}
	if d := now.Sub(wall); otaStepBackRefused.Load() || d > otaMaxStepBack {
		return fmt.Errorf("манифест %s выпущен %s, старше %d дней по отметке часов этой загрузки (%s UTC), а часы ноды %s UTC "+
			"отвели назад больше чем на %d ч после загрузки. Обновление продолжится после перезагрузки ноды или когда часы догонят отметку",
			m.Version, t.UTC().Format("2006-01-02"), int(otaMaxAge.Hours()/24), now.UTC().Format(tf), wall.UTC().Format(tf),
			int(otaMaxStepBack.Hours()))
	} else if d > time.Minute || d < -time.Minute {
		return fmt.Errorf("манифест %s выпущен %s, старше %d дней по отметке часов этой загрузки (%s UTC), а часы ноды %s UTC: "+
			"часы отводили назад или раньше они убегали вперед. Повторим; отметка сбросится после синхронизации NTP или перезагрузки",
			m.Version, t.UTC().Format("2006-01-02"), int(otaMaxAge.Hours()/24), now.UTC().Format(tf), wall.UTC().Format(tf))
	}
	return fmt.Errorf("манифест %s выпущен %s, старше %d дней по часам ноды (%s UTC) - отказ: нужен свежий выпуск или верные часы ноды",
		m.Version, t.UTC().Format("2006-01-02"), int(otaMaxAge.Hours()/24), wall.UTC().Format(tf))
}
