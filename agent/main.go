// Kervax agent — собирает метрики локально и шлёт их НАРУЖУ на панель по HTTPS.
// Чистый Go (без cgo) → статический бинарь, ноль зависимостей на сервере.
// Панель не имеет доступа к серверу: агент только POST-ит метрики своим токеном.
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const version = "1.97"

// Публичный ключ для проверки подписи релизов агента (Ed25519, base64).
// ПУСТО по умолчанию → самообновление ВЫКЛЮЧЕНО (агент никогда не заменяет себя).
// Значение инжектится при сборке: go build -ldflags "-X main.updatePubKeyB64=<pub>"
// (панель берёт его из KERVAX_AGENT_PUBKEY, релиз — из agent-signing/kervax-agent.pub).
// Приватная половина живёт ОФЛАЙН и никогда не попадает на панель — даже взломанная
// панель не подделает подпись. См. agent-signing/ и README.
var updatePubKeyB64 = ""

// разделы меньше этого не мониторим (мелкая системщина: /boot и т.п.)
const minDiskBytes = 2 << 30 // 2 ГиБ

// host:port панели — цель UDP-«дозвона» для выбора адреса-источника (локального IP)
var dialTarget string

// dialTargetFromURL — host:port из URL панели (порт по схеме, если не задан).
func dialTargetFromURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return net.JoinHostPort(u.Hostname(), port)
}

// localIP — адрес, которым агент дотягивается до панели. UDP-Dial не шлёт пакетов,
// но ядро выбирает исходящий интерфейс/адрес по таблице маршрутизации.
func localIP() string {
	if dialTarget == "" {
		return ""
	}
	c, err := net.Dial("udp", dialTarget)
	if err != nil {
		return ""
	}
	defer c.Close()
	if a, ok := c.LocalAddr().(*net.UDPAddr); ok {
		return a.IP.String()
	}
	return ""
}

type disk struct {
	Mount string `json:"mount"`
	Used  uint64 `json:"used"`
	Total uint64 `json:"total"`
}

type report struct {
	Hostname      string        `json:"hostname"`
	OS            string        `json:"os"`
	AgentVersion  string        `json:"agent_version"`
	CPUModel      string        `json:"cpu_model"` // напр. «AMD Ryzen 9 7900X3D»
	IsVM          bool          `json:"is_vm"`     // виртуалка ли (hypervisor-флаг/DMI)
	Virt          string        `json:"virt"`      // тип гипервизора: Hyper-V/KVM/VMware/… ('' = железо)
	LocalIP       string        `json:"local_ip"`
	Uptime        int64         `json:"uptime_seconds"`
	CPUPercent    float64       `json:"cpu_percent"`
	MemUsed       uint64        `json:"mem_used"`
	MemTotal      uint64        `json:"mem_total"`
	SwapUsed      uint64        `json:"swap_used"`
	SwapTotal     uint64        `json:"swap_total"`
	Load          []float64     `json:"load"`
	Disks         []disk        `json:"disks"`
	DBEngines     []string      `json:"db_engines,omitempty"`   // СУБД на ноде (нужен дамп, не файловый снапшот)
	Services      []serviceInfo `json:"services,omitempty"`     // прикладные метрики (очереди RabbitMQ и т.п.)
	WebServices   []webService  `json:"web_services,omitempty"` // веб-серверы/прокси (nginx/envoy/…) + сайты
	DBStats       []dbStat      `json:"db_stats,omitempty"`     // инвентарь СУБД: базы, размеры, логины (root-хелпер dbstat-setup)
	NetRx         float64       `json:"net_rx"`                 // байт/сек
	NetTx         float64       `json:"net_tx"`
	DiskRead      float64       `json:"disk_read"`       // байт/сек, чтение с дисков
	DiskWrite     float64       `json:"disk_write"`      // байт/сек, запись на диски
	DiskReadIOPS  float64       `json:"disk_read_iops"`  // операций чтения/сек
	DiskWriteIOPS float64       `json:"disk_write_iops"` // операций записи/сек
	// разбивка CPU по состояниям, % (для стек-графика)
	CPUCores  int     `json:"cpu_cores"`
	CPUUser   float64 `json:"cpu_user"`
	CPUSystem float64 `json:"cpu_system"`
	CPUIowait float64 `json:"cpu_iowait"`
	CPUIrq    float64 `json:"cpu_irq"`
	// per-core загрузка + частота/температура/троттлинг (null-поля = датчика нет, напр. VM)
	CPUCoresPct []float64 `json:"cpu_cores_pct"`
	CPUFreqMHz  *float64  `json:"cpu_freq"`     // средняя частота, МГц
	CPUTemp     *float64  `json:"cpu_temp"`     // температура CPU, °C
	CPUThrottle *float64  `json:"cpu_throttle"` // тепловых троттлингов за интервал
	OOMKill     *float64  `json:"oom_kill"`     // OOM-киллов за интервал (нехватка памяти)
	OOMVictim   string    `json:"oom_victim"`   // имя последнего OOM-убитого процесса (kmsg)
	// память, байты (панель считает %): used=total-avail, cache=buffers+cached, free
	MemCached uint64 `json:"mem_cached"`
	MemFree   uint64 `json:"mem_free"`
	// swap-активность (байт/сек) + детальная разбивка памяти (байты)
	SwapIn       float64 `json:"swap_in"`       // подкачано с диска
	SwapOut      float64 `json:"swap_out"`      // вытеснено на диск
	MemSlab      uint64  `json:"mem_slab"`      // память ядра (slab-аллокатор)
	MemDirty     uint64  `json:"mem_dirty"`     // «грязные» страницы (ждут записи)
	MemWriteback uint64  `json:"mem_writeback"` // активно пишутся на диск
	// разбивка по интерфейсам/устройствам (для overlay-графиков)
	NetIfaces []ifaceRate `json:"net_ifaces"` // rx/tx по каждому NIC, байт/сек
	DiskDevs  []devRate   `json:"disk_devs"`  // %util/await/°C по каждому диску
	// топ-процессы (снапшот): по CPU и по памяти
	TopCPU []procStat `json:"top_cpu"`
	TopMem []procStat `json:"top_mem"`
	// таблица conntrack + сокеты (мгновенные значения)
	ConntrackCount float64 `json:"conntrack_count"`
	ConntrackMax   float64 `json:"conntrack_max"`
	SockUsed       float64 `json:"sock_used"`   // всего сокетов (v4)
	SockTCP        float64 `json:"sock_tcp"`    // TCP inuse (v4+v6)
	SockTW         float64 `json:"sock_tcp_tw"` // TCP time-wait (v4)
	SockUDP        float64 `json:"sock_udp"`    // UDP inuse (v4+v6)
	// самодиагностика прав, зависящих от systemd-ЮНИТА (не от бинаря — OTA юнит не
	// меняет). Панель по ним подсказывает, что дописать в юнит на ноде. Расширяемо:
	// новая фича, требующая юнита → новый ключ здесь + в install.sh + в реестре панели.
	Caps map[string]bool `json:"caps,omitempty"`
	// Docker на ноде (nil = не установлен). Present без Access = docker есть, но
	// агент не видит сокет (нет доступа) → панель подскажет безопасную настройку.
	Docker *dockerInfo `json:"docker,omitempty"`
	// Kubernetes на ноде (nil = не установлен). Present без Access = кластер есть,
	// но нет /etc/kervax/kube.json (SA-токена) → панель подскажет kube-setup.sh.
	Kube *kubeInfo `json:"kube,omitempty"`
	// restic-бэкап на ноде (nil = следов не найдено). Статус снимается unprivileged:
	// метрики node_exporter (rk.prom) + read-only `systemctl show/is-*`.
	Backup *backupInfo `json:"backup,omitempty"`
	// сервер бэкапов (rest-server) на ноде (nil = не бэкап-сервер). Детект по docker;
	// per-repo статистика — через helper (backupserver-setup.sh), без паролей.
	BackupServer *backupServerInfo `json:"backup_server,omitempty"`
	// версии установленных setup-скриптов ({backup-setup:1, kube-setup:1, …}) — панель
	// сверяет с раздаваемыми и флагует устаревшие helper'ы (нужен ручной re-install).
	SetupVersions map[string]string `json:"setup_versions,omitempty"`
	// часы: статус синхронизации (timedatectl, unprivileged) + локальное wall-clock время
	// на момент отправки. Панель по clock_unix считает РЕАЛЬНЫЙ сдвиг относительно своих
	// (точных) часов — работает даже если у ноды закрыт исходящий и до NTP не достучаться.
	Clock     *clockInfo `json:"clock,omitempty"`
	ClockUnix int64      `json:"clock_unix,omitempty"`
}

// статус синхронизации времени (unprivileged: timedatectl + is-active демона). Сам оффсет
// от NTP-демона не читаем — авторитетный сдвиг панель меряет по clock_unix; этот блок лишь
// объясняет ПОЧЕМУ (нет живого демона / NTP выключен / не синхронизировано).
type clockInfo struct {
	Synced  bool   `json:"synced"`            // NTPSynchronized=yes
	NTP     bool   `json:"ntp"`               // NTP=yes (синхронизация включена)
	Service string `json:"service,omitempty"` // активный демон времени (systemd-timesyncd/chronyd/…)
}

// снимок сервера бэкапов (rest-server). Репозитории видим как папки (config+snapshots),
// свежесть = mtime самого свежего файла — без restic/паролей. Валидность/полнота.
type backupServerInfo struct {
	Present       bool   `json:"present"`
	Running       bool   `json:"running"` // контейнер rest-server запущен
	Version       string `json:"version,omitempty"`
	HelperVersion int    `json:"helper_version,omitempty"` // версия backupserver-helper (панель флагует старые)
	TLSFront      bool   `json:"tls_front,omitempty"`      // поднят self-signed TLS-фронт (caddy) → клиенты могут по HTTPS
	TLSPort       int    `json:"tls_port,omitempty"`       // порт TLS-фронта (обычно 64101)
	// место на томе С РЕПОЗИТОРИЯМИ (df по каталогу данных, снимает root-helper):
	// репозитории часто лежат на отдельном диске, и общая метрика диска ноды про
	// заполнение хранилища бэкапов ничего не говорит
	DataDir   string     `json:"data_dir,omitempty"`
	DiskTotal int64      `json:"disk_total,omitempty"`
	DiskUsed  int64      `json:"disk_used,omitempty"`
	DiskFree  int64      `json:"disk_free,omitempty"`
	Repos     []repoStat `json:"repos,omitempty"`
}

type repoStat struct {
	Name         string `json:"name"`
	Valid        bool   `json:"valid"` // есть config = валидный restic-репо
	SizeBytes    int64  `json:"size_bytes,omitempty"`
	Snapshots    int    `json:"snapshots"`
	LastActivity int64  `json:"last_activity,omitempty"` // epoch mtime самого свежего снапшота
	Locked       bool   `json:"locked,omitempty"`
	LockTs       int64  `json:"lock_ts,omitempty"` // mtime свежего лока: живой бэкап освежает его раз в 5 мин
	// Состояние РОТАЦИИ (метрики prune-скрипта, backupserver-setup 0.19+). Отдельно от
	// снапшотов и размера: «бэкап снялся» и «старое вычищается» — разные величины, и
	// именно вторую система раньше не мерила вовсе.
	RotationTs      int64 `json:"rotation_ts,omitempty"`     // когда чистка отрабатывала
	RotationOK      int   `json:"rotation_ok"`               // -1 нет данных, 0/1 — rc команд
	RotationRemoved int   `json:"rotation_removed"`          // снапшотов удалено за прогон (-1 нет данных)
	OldestSnapshot  int64 `json:"oldest_snapshot,omitempty"` // время самого старого снапшота
	// политика хранения из prune-скрипта (без паролей): сколько держим копий
	KeepLast    int `json:"keep_last,omitempty"`
	KeepDaily   int `json:"keep_daily,omitempty"`
	KeepWeekly  int `json:"keep_weekly,omitempty"`
	KeepMonthly int `json:"keep_monthly,omitempty"`
}

// снимок статуса restic-бэкапа (read-only, без секретов). Обфускация клиента
// сохраняется — панель видит только «бэкапится/свежесть/успех», не пароли/сервер.
type backupInfo struct {
	Present       bool   `json:"present"`
	ResticFound   bool   `json:"restic_found"`
	ResticVersion string `json:"restic_version,omitempty"`
	Configured    bool   `json:"configured"` // есть systemd timer/service бэкапа
	TimerEnabled  bool   `json:"timer_enabled"`
	TimerActive   bool   `json:"timer_active"`
	ServiceResult string `json:"service_result,omitempty"` // success/failed/… (последний прогон)
	MetricPresent bool   `json:"metric_present"`
	Success       *int   `json:"success,omitempty"`        // 1/0 из метрики (nil = метрики нет)
	Skipped       int    `json:"skipped,omitempty"`        // прошлый прогон пропущен (лок занят)
	LastBackupTs  int64  `json:"last_backup_ts,omitempty"` // epoch последнего бэкапа (метрика)
	DurationSec   int64  `json:"duration_sec,omitempty"`   // длительность restic (без дампов)
	// полная длительность запуска сервиса: дампы (ExecStartPre) + restic. Позволяет понять,
	// во сколько реально закончился бэкап, а не только restic-фаза. Из systemd (моно-метки).
	FullDurationSec int64      `json:"full_duration_sec,omitempty"`
	StartedTs       int64      `json:"started_ts,omitempty"` // epoch старта последнего запуска (с дампами)
	TsSource        string     `json:"ts_source,omitempty"`  // "" = из метрики, "systemd" = фолбэк по юниту
	Dumps           []dumpStat `json:"dumps,omitempty"`      // включённые дампы СУБД (состояние, не разовый ответ)
	Notes           []string   `json:"notes,omitempty"`
	// управление (Фаза 2): доступно, если установлен привилегированный helper
	// (backup-setup.sh → /lib65/kervax/kervax-backup-helper + sudoers). БЕЗ секретов.
	Manageable    bool     `json:"manageable"`
	Mode          string   `json:"mode,omitempty"`     // include/exclude
	Schedule      string   `json:"schedule,omitempty"` // HH:MM
	Includes      []string `json:"includes,omitempty"`
	Excludes      []string `json:"excludes,omitempty"`
	RepoDest      string   `json:"repo_dest,omitempty"`      // куда бэкапится (rest://host:port/name БЕЗ пароля)
	HelperVersion int      `json:"helper_version,omitempty"` // версия backup-helper (панель флагует старые)
}

// снимок Kubernetes на ноде (read-only). Агент ходит в kube-API по токену
// выделенного ServiceAccount с УЗКИМ RBAC (см. kube-setup.sh) — не cluster-admin.
type kubeInfo struct {
	Present      bool           `json:"present"`
	Access       bool           `json:"access"`            // прочитан kube.json и опрошен kube-api
	Flavor       string         `json:"flavor,omitempty"`  // k0s/k3s/microk8s/kubeadm/kubernetes
	Version      string         `json:"version,omitempty"` // серверная k8s-версия (gitVersion)
	Nodes        []kubeNode     `json:"nodes,omitempty"`
	Namespaces   int            `json:"namespaces,omitempty"`
	Workloads    []kubeWorkload `json:"workloads,omitempty"`
	Pods         []kubePod      `json:"pods,omitempty"`
	CronJobs     []kubeCronJob  `json:"cronjobs,omitempty"` // панель по ним видит уже настроенные дампы
	Volumes      []kubeVolume   `json:"volumes,omitempty"`  // тома: панель сверяет их с покрытием бэкапа
	ingressHosts []string       `json:"-"`                  // хосты из Ingress → вешаем на web_services, в отчёт не дублируем
}

// kubeVolume — постоянный том кластера. Панели важно ОДНО: лежат ли данные каталогом на
// этой ноде (hostPath/local — restic заберёт их файловым бэкапом) или снаружи (nfs/csi —
// не заберёт никогда, нужен свой механизм). Содержимого томов не читаем, только спеку.
type kubeVolume struct {
	Name     string `json:"name"`
	Claim    string `json:"claim,omitempty"` // ns/name PVC — по нему человек узнаёт «чей» том
	Kind     string `json:"kind"`            // hostPath/local/nfs/csi/…
	Path     string `json:"path,omitempty"`  // каталог на ноде (только hostPath/local)
	Node     string `json:"node,omitempty"`  // нода из nodeAffinity: том локален для НЕЁ
	Capacity string `json:"capacity,omitempty"`
	Class    string `json:"class,omitempty"`
}

// kubeCronJob — расписание задания. Нужен, чтобы не ныть «настройте дамп» там, где он
// давно настроен. Секретов НЕ содержит: только имена/расписание/образ.
type kubeCronJob struct {
	NS       string `json:"ns"`
	Name     string `json:"name"`
	Schedule string `json:"schedule,omitempty"`
	Suspend  bool   `json:"suspend,omitempty"`
	Image    string `json:"image,omitempty"`
	// статус прогонов — для мониторинга дамп-CronJob'ов (панель алертит на сбой/несвежесть)
	LastSchedule int64 `json:"last_schedule,omitempty"` // unix: последний запуск по расписанию
	LastSuccess  int64 `json:"last_success,omitempty"`  // unix: последний УСПЕШНЫЙ прогон
	Active       int   `json:"active,omitempty"`        // сколько Job'ов сейчас выполняется
}

type kubeNode struct {
	Name    string `json:"name"`
	Ready   bool   `json:"ready"`
	Roles   string `json:"roles,omitempty"`   // control-plane/worker
	Version string `json:"version,omitempty"` // версия kubelet
	IP      string `json:"ip,omitempty"`
}

type kubeWorkload struct {
	NS      string `json:"ns"`
	Kind    string `json:"kind"` // Deployment/StatefulSet/DaemonSet
	Name    string `json:"name"`
	Ready   int    `json:"ready"`
	Desired int    `json:"desired"`
}

type kubePod struct {
	NS       string    `json:"ns"`
	Name     string    `json:"name"`
	Phase    string    `json:"phase"`    // Running/Pending/Succeeded/Failed/Unknown
	Ready    bool      `json:"ready"`    // все контейнеры ready
	Restarts int       `json:"restarts"` // сумма restartCount
	Node     string    `json:"node,omitempty"`
	Reason   string    `json:"reason,omitempty"` // CrashLoopBackOff/ImagePullBackOff/OOMKilled…
	Owner    string    `json:"owner,omitempty"`  // kind контроллера (Job/ReplicaSet/StatefulSet/DaemonSet/Node) — отличить историю Job'ов от живых воркоадов
	Image    string    `json:"image,omitempty"`  // заполняется ТОЛЬКО у СУБД-подов (аудит бэкапа)
	Cred     *kubeCred `json:"cred,omitempty"`   // откуда СУБД-под берёт креды (ССЫЛКИ, не значения) — для автоподстановки в манифест дампа
	ip       string    // podIP: нужен агенту для скрейпа метрик, в отчёт НЕ уходит (строчная = не сериализуется)
}

// kubeCred — как СУБД-под получает логин/пароль. Панель воспроизведёт это в CronJob-дампе
// ТЕМ ЖЕ способом (envFrom/secretKeyRef) — секреты не читаются и не хранятся, шлём только
// ССЫЛКИ (имена секретов/ключей) и plain-значения НЕсекретных переменных (user/database).
type kubeCred struct {
	EnvFrom []string     `json:"env_from,omitempty"` // secretRef из envFrom (весь секрет оптом)
	Env     []kubeEnvRef `json:"env,omitempty"`      // кред-переменные: имя + источник
}

type kubeEnvRef struct {
	Name   string `json:"name"`             // MARIADB_PASSWORD, POSTGRES_USER, …
	Value  string `json:"value,omitempty"`  // plain-значение — ТОЛЬКО у НЕсекретных (user/database); пароли сюда НЕ попадают
	Secret string `json:"secret,omitempty"` // secretKeyRef.name
	Key    string `json:"key,omitempty"`    // secretKeyRef.key
}

// ---- сервисы: прикладные метрики, которые видны БЕЗ кредов и без exec ----

type queueStat struct {
	Name    string `json:"name"`
	VHost   string `json:"vhost,omitempty"`
	Ready   int64  `json:"ready"`
	Unacked int64  `json:"unacked,omitempty"`
}

type serviceInfo struct {
	Kind   string      `json:"kind"`             // rabbitmq
	Source string      `json:"source,omitempty"` // «под ns/name» или «контейнер name»
	Queues []queueStat `json:"queues,omitempty"`
	Total  int         `json:"total,omitempty"` // всего очередей (список может быть урезан)
}

// сколько очередей максимум шлём: у клиента их 77, гонять все в КАЖДОМ отчёте — мусор.
// Шлём непустые, добивая пустыми до лимита (чтобы список не выглядел пустым на простое).
const maxQueuesReported = 40

var reQueueMetric = regexp.MustCompile(
	`^rabbitmq_detailed_queue_messages_(ready|unacked)\{([^}]*)\}\s+([0-9.e+]+)`)

// rabbitQueues — очереди инстанса RabbitMQ через prometheus-плагин (порт 15692).
// Именно он, а не management API: /metrics отдаётся БЕЗ авторизации, т.е. панели не нужны
// ни пароли, ни kubectl exec. Если плагин не включён — просто вернём пусто.
func rabbitQueues(ip string) ([]queueStat, int) {
	cl := &http.Client{Timeout: 4 * time.Second}
	url := "http://" + ip + ":15692/metrics/detailed?family=queue_coarse_metrics"
	resp, err := cl.Get(url)
	if err != nil {
		return nil, 0
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, 0
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, 0
	}
	idx := map[string]*queueStat{}
	for _, ln := range strings.Split(string(body), "\n") {
		m := reQueueMetric.FindStringSubmatch(ln)
		if m == nil {
			continue
		}
		var vhost, queue string
		for _, kv := range strings.Split(m[2], ",") {
			k, v, ok := strings.Cut(kv, "=")
			if !ok {
				continue
			}
			v = strings.Trim(v, `"`)
			switch strings.TrimSpace(k) {
			case "vhost":
				vhost = v
			case "queue":
				queue = v
			}
		}
		if queue == "" {
			continue
		}
		val, _ := strconv.ParseFloat(m[3], 64)
		key := vhost + "\x00" + queue
		q := idx[key]
		if q == nil {
			q = &queueStat{Name: queue, VHost: vhost}
			idx[key] = q
		}
		if m[1] == "ready" {
			q.Ready = int64(val)
		} else {
			q.Unacked = int64(val)
		}
	}
	all := make([]queueStat, 0, len(idx))
	for _, q := range idx {
		all = append(all, *q)
	}
	// сначала самые глубокие: если список урежется, потеряем пустые, а не проблемные
	sort.Slice(all, func(i, j int) bool {
		a, b := all[i].Ready+all[i].Unacked, all[j].Ready+all[j].Unacked
		if a != b {
			return a > b
		}
		return all[i].Name < all[j].Name
	})
	total := len(all)
	if len(all) > maxQueuesReported {
		all = all[:maxQueuesReported]
	}
	return all, total
}

// collectServices — прикладные метрики сервисов, которые читаются БЕЗ секретов и без
// exec. Сейчас это RabbitMQ (prometheus-плагин на 15692). Инстансы берём из уже собранных
// docker-контейнеров и kube-подов, IP — их внутренние (в отчёт IP не уходит).
func collectServices(dk *dockerInfo, kube *kubeInfo) []serviceInfo {
	var out []serviceInfo
	add := func(kind, source, ip string) {
		if ip == "" {
			return
		}
		if qs, total := rabbitQueues(ip); total > 0 {
			out = append(out, serviceInfo{Kind: kind, Source: source, Queues: qs, Total: total})
		}
	}
	if dk != nil && dk.Access {
		for _, c := range dk.Containers {
			if c.State == "running" && strings.Contains(strings.ToLower(c.Image), "rabbitmq") {
				add("rabbitmq", "контейнер "+c.Name, c.ip)
			}
		}
	}
	if kube != nil && kube.Access {
		for _, p := range kube.Pods {
			if p.Phase == "Running" && strings.Contains(strings.ToLower(p.Image), "rabbitmq") {
				add("rabbitmq", "под "+p.NS+"/"+p.Name, p.ip)
			}
		}
	}
	return out
}

// dbImageLike — грубый фильтр «похоже на СУБД» для образов подов. Намеренно широкий:
// точное сопоставление образ→движок делает панель, ей же проще добавлять новые движки.
func dbImageLike(image string) bool {
	l := strings.ToLower(image)
	// prometheus-экспортеры (postgres-exporter, redis-exporter, mysqld-exporter…) содержат
	// имя движка в образе, но САМИ данных не хранят — только читают метрики из базы.
	// Считать их СУБД = ложная находка «нужен дамп». То же для postgrest (REST-обёртка).
	if strings.Contains(l, "exporter") || strings.Contains(l, "postgrest") {
		return false
	}
	for _, k := range []string{
		"postgres", "postgis", "timescale", "mysql", "mariadb", "percona", "mongo",
		"clickhouse", "elasticsearch", "opensearch", "redis", "valkey", "influxdb",
		"victoriametrics", "etcd", "cockroach", "cassandra", "zookeeper", "kafka",
		"neo4j", "rabbitmq", "couchdb", "mssql", "sqlserver", "minio", "vault",
		"keydb", "dragonfly", "scylla", "arango", "rethinkdb", "memcached", "surreal",
	} {
		if strings.Contains(l, k) {
			return true
		}
	}
	return false
}

// снимок Docker на ноде (read-only)
type dockerInfo struct {
	Present    bool              `json:"present"`
	Access     bool              `json:"access"` // агент смог прочитать сокет
	Version    string            `json:"version,omitempty"`
	APIVersion string            `json:"api_version,omitempty"`
	Compose    string            `json:"compose,omitempty"`
	Containers []dockerContainer `json:"containers,omitempty"`
}

type dockerContainer struct {
	Name     string   `json:"name"`
	Image    string   `json:"image"`
	State    string   `json:"state"`            // running / exited / paused / restarting / created
	Status   string   `json:"status"`           // «Up 3 hours (healthy)» / «Exited (0) 2 days ago»
	Restarts int      `json:"restarts"`         // RestartCount демона — детект crash-loop на бэкенде
	Policy   string   `json:"policy,omitempty"` // restart-policy: no/always/unless-stopped/on-failure
	Health   string   `json:"health,omitempty"` // healthy/unhealthy/starting (если есть healthcheck)
	Binds    []string `json:"binds,omitempty"`  // хост-пути bind-mount'ов (аудит покрытия бэкапа)
	ip       string   // IP контейнера для скрейпа метрик; в отчёт НЕ уходит (строчная)
}

// пропускная способность одного сетевого интерфейса, байт/сек + ошибки/дропы, пакетов/сек
type ifaceRate struct {
	If    string  `json:"if"`
	Rx    float64 `json:"rx"`
	Tx    float64 `json:"tx"`
	Errs  float64 `json:"errs"`  // rx+tx ошибок в секунду
	Drops float64 `json:"drops"` // rx+tx дропнутых пакетов в секунду
}

// загрузка/задержка/температура одного дискового устройства
type devRate struct {
	Dev   string   `json:"dev"`
	Util  float64  `json:"util"`  // % времени, когда диск был занят I/O (как iostat %util)
	Await float64  `json:"await"` // средняя задержка операции, мс (как iostat await)
	Temp  *float64 `json:"temp"`  // °C из hwmon (drivetemp/nvme); null = датчика нет (напр. VM)
}

// строка топа процессов (снапшот, не тайм-серия)
type procStat struct {
	Pid     int     `json:"pid"`
	Comm    string  `json:"comm"`
	Cmdline string  `json:"cmdline,omitempty"` // полная командная строка (обрезана)
	User    string  `json:"user,omitempty"`    // владелец процесса
	CPU     float64 `json:"cpu"`               // % одного ядра (как top, может быть >100 у многопоточных)
	RSS     uint64  `json:"rss"`               // резидентная память, байты
	Shared  uint64  `json:"shared,omitempty"`  // общая память (RssShmem: shared_buffers и т.п.), байты
	Threads int     `json:"threads,omitempty"` // число потоков
	State   string  `json:"state,omitempty"`   // состояние: R/S/D/Z/…
}

type cpuStat struct {
	user, system, iowait, irq, idle, total uint64
}

type config struct {
	Interval       int             `json:"interval"`
	Update         *updateWish     `json:"update"` // != nil → панель просит обновиться
	DockerCommands []dockerCommand `json:"docker_commands"`
	// Панель раздаёт очередь И в ответе на отчёт, И в /commands — кто первый спросил,
	// тот и забрал (при выдаче команда помечается running). Здесь был только docker:
	// kube- и backup-команды, попавшие в ответ на отчёт, агент молча выбрасывал, и они
	// навсегда зависали в running. На здоровых нодах опрос (раз в секунду) почти всегда
	// успевал первым и это не всплывало; на ноде с деградировавшим опросом чаще успевал
	// отчёт — и «обновить restic» из панели там не срабатывало никогда.
	KubeCommands   []kubeCommand   `json:"kube_commands"`
	BackupCommands []backupCommand `json:"backup_commands"`
}

// docker-действие из очереди панели (исполняется через read-only proxy)
type dockerCommand struct {
	ID        int    `json:"id"`
	Container string `json:"container"`
	Action    string `json:"action"` // restart / stop / start / logs
	Tail      int    `json:"tail"`
	Since     int    `json:"since"` // logs за последние N сек (0 = tail)
}

type kubeCommand struct {
	ID     int    `json:"id"`
	NS     string `json:"ns"`
	Kind   string `json:"kind"` // deployment/statefulset/daemonset/pod
	Name   string `json:"name"`
	Action string `json:"action"` // rollout_restart / delete_pod / logs
	Tail   int    `json:"tail"`
	Since  int    `json:"since"`
}

type backupCommand struct {
	ID       int      `json:"id"`
	Action   string   `json:"action"`   // set_paths/set_schedule/run_now/provision (клиент); deploy_server/provision_client/deploy_tls_front/get_cert (бэкап-сервер)
	Mode     string   `json:"mode"`     // include/exclude
	Paths    []string `json:"paths"`    // пути (set_paths/provision)
	Schedule string   `json:"schedule"` // HH:MM
	// provision (клиент, создать бэкап с нуля)
	RepoURL       string `json:"repo_url,omitempty"`
	Repopass      string `json:"repopass,omitempty"`
	Delay         string `json:"delay,omitempty"`
	ResticVersion string `json:"restic_version,omitempty"`
	CacertB64     string `json:"cacert_b64,omitempty"`
	// provision_client / deploy_tls_front (бэкап-сервер)
	Name        string `json:"name,omitempty"`
	Hpass       string `json:"hpass,omitempty"`
	ClientIP    string `json:"client_ip,omitempty"`
	KeepLast    int    `json:"keep_last,omitempty"`
	KeepDaily   int    `json:"keep_daily,omitempty"`
	KeepWeekly  int    `json:"keep_weekly,omitempty"`
	KeepMonthly int    `json:"keep_monthly,omitempty"`
	SanIP       string `json:"san_ip,omitempty"`
	SanDNS      string `json:"san_dns,omitempty"`
	Port        int    `json:"port,omitempty"`         // deploy_server: порт rest-server
	Engine      string `json:"engine,omitempty"`       // dump_setup: pg/mysql/ch
	Container   string `json:"container,omitempty"`    // dump_setup: имя контейнера ("" = нативно)
	DumpDir     string `json:"dump_dir,omitempty"`     // dump_setup: каталог дампов
	DumpKeep    int    `json:"dump_keep,omitempty"`    // dump_setup: сколько последних хранить
	DumpMinFree int    `json:"dump_minfree,omitempty"` // dump_setup: минимум свободного места, %
}

// панель кладёт это в ответ, когда для сервера выставлена target-версия
type updateWish struct {
	Version string `json:"version"`
}

// подписанный манифест релиза (ровно эти байты подписаны офлайн-ключом)
type artifact struct {
	SHA256 string `json:"sha256"`
	Size   int    `json:"size"`
}
type manifest struct {
	Version   string              `json:"version"`
	Artifacts map[string]artifact `json:"artifacts"`
}

// versionNewer — a строго новее b (сравнение по dotted-числам): анти-откат.
func versionNewer(a, b string) bool {
	pa, pb := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			return x > y
		}
	}
	return false
}

// Скачивание бинаря кусками. Одним куском 6 МБ на плохом канале не доходят: передача
// рвётся молча и всё начинается сначала — на ноде за DPI OTA не проходила вообще.
// Куском в мегабайт (при неудаче — вдвое меньше, вплоть до 64 КБ) доходит, а уже
// принятое не перекачивается. Каждый кусок — своё соединение: счётчик байт, по которому
// нас рубят, обнуляется (см. panelTransport).
// Замерено на ноде за DPI: обрыв — лотерея на КАЖДОЕ соединение (три куска по
// мегабайту из шести пришли целиком, остальные умерли на ~12 КБ), причём окна везения
// чередуются. Поэтому бюджет считаем по неудачам ПОДРЯД и обнуляем на каждом принятом
// куске: иначе общий счётчик выгорал раньше, чем файл собирался. На успехе возвращаем
// размер куска обратно вверх — не тащить весь файл по 64 КБ, когда канал ожил.
// одно самообновление за раз: отчёты идут каждые 15-30с, а докачка длится минутами
var updating atomic.Bool

const (
	dlChunkMax    = 1 << 20
	dlChunkMin    = 64 << 10
	dlFailsMax    = 8   // неудач ПОДРЯД, после которых сдаёмся
	dlAttemptsMax = 400 // страховка от бесконечного цикла
)

// fetchRange — один кусок [from..to]. noRange=true, если панель не умеет Range —
// тогда зовущий откатывается на обычное скачивание целиком.
func fetchRange(client *http.Client, u string, from, to int) (b []byte, noRange bool, err error) {
	req, err := http.NewRequest("GET", u, nil)
	if err != nil {
		return nil, false, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", from, to))
	resp, err := client.Do(req)
	if err != nil {
		return nil, false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusPartialContent {
		// 200 = отдали файл целиком, Range не поддержан; прочее — честная ошибка
		return nil, resp.StatusCode == http.StatusOK, fmt.Errorf("HTTP %d вместо 206", resp.StatusCode)
	}
	want := to - from + 1
	b, err = io.ReadAll(io.LimitReader(resp.Body, int64(want)))
	if err != nil {
		return nil, false, err
	}
	if len(b) != want {
		return nil, false, fmt.Errorf("кусок короче: %d из %d", len(b), want)
	}
	return b, false, nil
}

// httpGetChunked — скачивание с докачкой. size берём из ПОДПИСАННОГО манифеста, так что
// на него можно опираться при выделении памяти; sha256 всё равно проверяется зовущим.
func httpGetChunked(u string, size int) ([]byte, error) {
	if size <= 0 || size > 128<<20 {
		return nil, fmt.Errorf("нереальный размер артефакта: %d", size)
	}
	client := &http.Client{Timeout: 60 * time.Second, Transport: panelTransport(false)}
	buf := make([]byte, 0, size)
	chunk, fails, attempts := dlChunkMax, 0, 0
	for len(buf) < size {
		if attempts++; attempts > dlAttemptsMax {
			return nil, fmt.Errorf("слишком много попыток, взято %d из %d байт", len(buf), size)
		}
		end := len(buf) + chunk - 1
		if end > size-1 {
			end = size - 1
		}
		part, noRange, err := fetchRange(client, u, len(buf), end)
		if noRange {
			return httpGetLimited(u, int64(size)+1)
		}
		if err != nil {
			if fails++; fails >= dlFailsMax {
				return nil, fmt.Errorf("на %d/%d байт: %w", len(buf), size, err)
			}
			if chunk > dlChunkMin {
				chunk /= 2 // канал не тянет длинную передачу — идём мельче
			}
			continue
		}
		buf = append(buf, part...)
		fails = 0
		if chunk < dlChunkMax {
			chunk *= 2
		}
	}
	return buf, nil
}

// httpGetLimited — GET с таймаутом и лимитом тела (враждебная панель не заольёт память).
func httpGetLimited(u string, max int64) ([]byte, error) {
	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, max))
}

// selfUpdate — БЕЗОПАСНОЕ самообновление. Ставит новый бинарь ТОЛЬКО если:
//  1. подпись манифеста верна (вшитый пубключ);
//  2. версия в манифесте == запрошенной панелью И строго новее текущей (анти-откат);
//  3. sha256 и размер скачанного бинаря совпали с подписанным манифестом.
//
// Иначе — отказ, продолжаем работать на текущей версии. Замена атомарная,
// перезапуск через exec (тот же PID). Root не нужен: бинарь в каталоге агента-владельца.
func selfUpdate(panelURL, want string) error {
	base := strings.TrimRight(panelURL, "/")

	manBytes, err := httpGetLimited(base+"/api/agent/manifest", 1<<20)
	if err != nil {
		return fmt.Errorf("манифест: %w", err)
	}
	sigRaw, err := httpGetLimited(base+"/api/agent/manifest.sig", 4<<10)
	if err != nil {
		return fmt.Errorf("подпись: %w", err)
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigRaw)))
	if err != nil {
		return fmt.Errorf("подпись не base64: %w", err)
	}
	pub, err := base64.StdEncoding.DecodeString(updatePubKeyB64)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return fmt.Errorf("некорректный вшитый пубключ")
	}
	if !ed25519.Verify(ed25519.PublicKey(pub), manBytes, sig) {
		return fmt.Errorf("ПОДПИСЬ МАНИФЕСТА НЕВЕРНА — отказ (возможна подмена)")
	}

	var m manifest
	if err := json.Unmarshal(manBytes, &m); err != nil {
		return fmt.Errorf("манифест не JSON: %w", err)
	}
	if m.Version != want {
		return fmt.Errorf("панель просит %s, а подписан %s — отказ", want, m.Version)
	}
	if !versionNewer(m.Version, version) {
		return fmt.Errorf("%s не новее текущей %s — отказ (анти-откат)", m.Version, version)
	}
	art, ok := m.Artifacts[runtime.GOARCH]
	if !ok {
		return fmt.Errorf("в манифесте нет артефакта для %s", runtime.GOARCH)
	}

	bin, err := httpGetChunked(base+"/api/agent/download/"+runtime.GOARCH, art.Size)
	if err != nil {
		return fmt.Errorf("скачивание бинаря: %w", err)
	}
	sum := sha256.Sum256(bin)
	if hex.EncodeToString(sum[:]) != art.SHA256 {
		return fmt.Errorf("sha256 бинаря не совпал с подписанным — отказ (подмена)")
	}
	if len(bin) != art.Size {
		return fmt.Errorf("размер бинаря не совпал с подписанным — отказ")
	}

	self, err := os.Executable()
	if err != nil {
		return err
	}
	if resolved, e := filepath.EvalSymlinks(self); e == nil {
		self = resolved
	}
	tmp := filepath.Join(filepath.Dir(self), ".kervax-agent.new")
	if err := os.WriteFile(tmp, bin, 0o755); err != nil {
		return fmt.Errorf("запись %s: %w (нужны права на каталог агента)", tmp, err)
	}
	if err := os.Chmod(tmp, 0o755); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, self); err != nil { // атомарно, лечит ETXTBSY (свап inode)
		os.Remove(tmp)
		return fmt.Errorf("замена бинаря: %w", err)
	}
	fmt.Printf("kervax-agent: обновлён %s → %s, перезапуск\n", version, m.Version)
	return syscall.Exec(self, os.Args, os.Environ()) // тот же PID, systemd доволен
}

// --- сбор метрик из /proc и statfs ---

func cpuFromFields(f []string) cpuStat {
	p := make([]uint64, 10)
	for i, s := range f {
		if i >= 10 {
			break
		}
		p[i], _ = strconv.ParseUint(s, 10, 64)
	}
	var c cpuStat
	for _, v := range p {
		c.total += v
	}
	c.user = p[0] + p[1] // user + nice
	c.system = p[2]      // system
	c.idle = p[3]        // idle
	c.iowait = p[4]      // iowait
	c.irq = p[5] + p[6]  // irq + softirq
	return c
}

// readCPUAll — агрегат «cpu » + per-core «cpu0/cpu1/…» из /proc/stat одним чтением.
func readCPUAll() (agg cpuStat, cores []cpuStat) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 2 || !strings.HasPrefix(fields[0], "cpu") {
			continue
		}
		if fields[0] == "cpu" {
			agg = cpuFromFields(fields[1:])
		} else { // cpu0, cpu1, …
			cores = append(cores, cpuFromFields(fields[1:]))
		}
	}
	return
}

func round1(x float64) float64 { return float64(int64(x*10+0.5)) / 10 }

// busyPct — загрузка ядра (%) по дельте: 100 - idle%.
func busyPct(cur, prev cpuStat) float64 {
	dt := float64(cur.total - prev.total)
	if dt <= 0 {
		return 0
	}
	idle := 0.0
	if cur.idle >= prev.idle {
		idle = float64(cur.idle-prev.idle) / dt * 100
	}
	return 100 - idle
}

// readFreqMHz — средняя текущая частота ядер (МГц) из /proc/cpuinfo «cpu MHz».
func readFreqMHz() (float64, bool) {
	f, err := os.Open("/proc/cpuinfo")
	if err != nil {
		return 0, false
	}
	defer f.Close()
	var sum float64
	var n int
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if !strings.HasPrefix(sc.Text(), "cpu MHz") {
			continue
		}
		if _, v, ok := strings.Cut(sc.Text(), ":"); ok {
			if x, e := strconv.ParseFloat(strings.TrimSpace(v), 64); e == nil {
				sum += x
				n++
			}
		}
	}
	if n == 0 {
		return 0, false
	}
	return sum / float64(n), true
}

func readMilliC(path string) float64 {
	b, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
	if err != nil {
		return -1
	}
	return v / 1000.0 // милли-°C → °C
}

// readTempC — температура CPU (°C): hwmon coretemp/k10temp/cpu_thermal или
// thermal_zone с CPU-типом. На VM обычно НЕТ датчика → (0, false).
func readTempC() (float64, bool) {
	best := -1.0
	if ents, err := os.ReadDir("/sys/class/hwmon"); err == nil {
		for _, e := range ents {
			base := "/sys/class/hwmon/" + e.Name()
			nb, _ := os.ReadFile(base + "/name")
			nm := strings.TrimSpace(string(nb))
			if nm == "coretemp" || nm == "k10temp" || strings.Contains(nm, "cpu") {
				if t := readMilliC(base + "/temp1_input"); t > best {
					best = t
				}
			}
		}
	}
	if zones, err := os.ReadDir("/sys/class/thermal"); err == nil {
		for _, z := range zones {
			if !strings.HasPrefix(z.Name(), "thermal_zone") {
				continue
			}
			base := "/sys/class/thermal/" + z.Name()
			tb, _ := os.ReadFile(base + "/type")
			ty := strings.ToLower(strings.TrimSpace(string(tb)))
			if strings.Contains(ty, "cpu") || strings.Contains(ty, "pkg") ||
				strings.Contains(ty, "core") || strings.Contains(ty, "x86") {
				if t := readMilliC(base + "/temp"); t > best {
					best = t
				}
			}
		}
	}
	if best < 0 {
		return 0, false
	}
	return best, true
}

// readThrottleCount — суммарный накопительный счётчик тепловых троттлингов по всем
// ядрам (core+package). На VM файлов нет → (0, false).
func readThrottleCount() (uint64, bool) {
	ents, err := os.ReadDir("/sys/devices/system/cpu")
	if err != nil {
		return 0, false
	}
	var sum uint64
	found := false
	for _, e := range ents {
		if !strings.HasPrefix(e.Name(), "cpu") {
			continue
		}
		tt := "/sys/devices/system/cpu/" + e.Name() + "/thermal_throttle/"
		for _, name := range []string{"core_throttle_count", "package_throttle_count"} {
			if b, err := os.ReadFile(tt + name); err == nil {
				if v, err := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64); err == nil {
					sum += v
					found = true
				}
			}
		}
	}
	return sum, found
}

func meminfo() map[string]uint64 {
	m := map[string]uint64{}
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return m
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		parts := strings.Fields(sc.Text())
		if len(parts) < 2 {
			continue
		}
		key := strings.TrimSuffix(parts[0], ":")
		v, _ := strconv.ParseUint(parts[1], 10, 64)
		m[key] = v * 1024 // кБ → байты
	}
	return m
}

// readVmstat — накопительные счётчики страниц swap in/out и OOM-киллов (/proc/vmstat).
// oomOK=false, если поля oom_kill нет (старое ядро <4.13).
func readVmstat() (swpin, swpout, oom uint64, oomOK bool) {
	f, err := os.Open("/proc/vmstat")
	if err != nil {
		return
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		k, v, ok := strings.Cut(sc.Text(), " ")
		if !ok {
			continue
		}
		switch k {
		case "pswpin":
			swpin, _ = strconv.ParseUint(strings.TrimSpace(v), 10, 64)
		case "pswpout":
			swpout, _ = strconv.ParseUint(strings.TrimSpace(v), 10, 64)
		case "oom_kill":
			oom, _ = strconv.ParseUint(strings.TrimSpace(v), 10, 64)
			oomOK = true
		}
	}
	return
}

// readOOMVictim — имя последнего OOM-убитого процесса из кольцевого буфера ядра
// (/dev/kmsg). Best-effort: без CAP_SYSLOG при dmesg_restrict=1 вернёт "" — тогда
// панель покажет алерт без имени. Юнит агента даёт AmbientCapabilities=CAP_SYSLOG.
func readOOMVictim() string {
	f, err := os.OpenFile("/dev/kmsg", os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return ""
	}
	defer f.Close()
	buf := make([]byte, 8192)
	victim := ""
	for {
		n, err := f.Read(buf)
		if err != nil { // EAGAIN (буфер вычитан) / EOF
			break
		}
		line := string(buf[:n])
		if i := strings.IndexByte(line, ';'); i >= 0 { // текст после метаданных
			line = line[i+1:]
		}
		if v := parseOOMVictim(line); v != "" {
			victim = v // берём ПОСЛЕДНИЙ (самый свежий)
		}
	}
	return victim
}

func parseOOMVictim(s string) string {
	// «Out of memory: Killed process 1234 (mysqld) …» / «Killed process 1234 (comm)»
	if idx := strings.Index(s, "Killed process "); idx >= 0 {
		rest := strings.TrimSpace(s[idx+len("Killed process "):])
		parts := strings.SplitN(rest, " ", 2)
		pid, comm := parts[0], ""
		if len(parts) > 1 {
			if a := strings.IndexByte(parts[1], '('); a >= 0 {
				if b := strings.IndexByte(parts[1][a+1:], ')'); b >= 0 {
					comm = parts[1][a+1 : a+1+b]
				}
			}
		}
		if comm != "" {
			return fmt.Sprintf("%s (pid %s)", comm, pid)
		}
	}
	// cgroup v2: «oom-kill:…,task=mysqld,pid=1234,…»
	if strings.Contains(s, "oom-kill:") {
		task, pid := oomKV(s, "task="), oomKV(s, "pid=")
		if task != "" {
			if pid != "" {
				return fmt.Sprintf("%s (pid %s)", task, pid)
			}
			return task
		}
	}
	return ""
}

func oomKV(s, key string) string {
	i := strings.Index(s, key)
	if i < 0 {
		return ""
	}
	rest := s[i+len(key):]
	if end := strings.IndexAny(rest, ", \n"); end >= 0 {
		return rest[:end]
	}
	return rest
}

func loadavg() []float64 {
	b, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return nil
	}
	parts := strings.Fields(string(b))
	out := make([]float64, 0, 3)
	for i := 0; i < 3 && i < len(parts); i++ {
		v, _ := strconv.ParseFloat(parts[i], 64)
		out = append(out, v)
	}
	return out
}

// суммарные счётчики rx/tx по всем интерфейсам кроме lo (/proc/net/dev)
func readNet() (rx, tx uint64) {
	f, err := os.Open("/proc/net/dev")
	if err != nil {
		return
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		iface, data, ok := strings.Cut(line, ":")
		if !ok {
			continue // строки-заголовки без ":"
		}
		if strings.TrimSpace(iface) == "lo" {
			continue
		}
		fields := strings.Fields(data)
		if len(fields) < 9 {
			continue
		}
		r, _ := strconv.ParseUint(fields[0], 10, 64) // receive bytes
		t, _ := strconv.ParseUint(fields[8], 10, 64) // transmit bytes
		rx += r
		tx += t
	}
	return
}

type netCtr struct{ rx, tx, errs, drops uint64 }

// счётчики rx/tx + ошибки/дропы по каждому интерфейсу (/proc/net/dev),
// кроме lo и veth-пар docker'а. Порядок полей (после «iface:»):
// rx: bytes packets errs drop … | tx: bytes packets errs drop …
func readNetIfaces() map[string]netCtr {
	m := map[string]netCtr{}
	f, err := os.Open("/proc/net/dev")
	if err != nil {
		return m
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		iface, data, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		name := strings.TrimSpace(iface)
		if name == "lo" || strings.HasPrefix(name, "veth") {
			continue
		}
		fields := strings.Fields(data)
		if len(fields) < 9 {
			continue
		}
		r, _ := strconv.ParseUint(fields[0], 10, 64)
		t, _ := strconv.ParseUint(fields[8], 10, 64)
		var errs, drops uint64
		if len(fields) >= 12 {
			re, _ := strconv.ParseUint(fields[2], 10, 64)  // rx errs
			rd, _ := strconv.ParseUint(fields[3], 10, 64)  // rx drop
			te, _ := strconv.ParseUint(fields[10], 10, 64) // tx errs
			td, _ := strconv.ParseUint(fields[11], 10, 64) // tx drop
			errs, drops = re+te, rd+td
		}
		m[name] = netCtr{r, t, errs, drops}
	}
	return m
}

// виртуальные/служебные устройства, которые не считаем за «диск»
func skipDiskDev(name string) bool {
	for _, p := range []string{"loop", "ram", "fd", "sr", "dm-", "md", "nbd", "zram"} {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

// раздел ли это (sda1 при наличии sda; nvme0n1p1 при наличии nvme0n1) — чтобы не
// считать I/O дважды. Цельные диски (sda, vda, nvme0n1, mmcblk0) — не разделы.
func isPartition(name string, all map[string]bool) bool {
	i := len(name)
	for i > 0 && name[i-1] >= '0' && name[i-1] <= '9' {
		i--
	}
	if i == len(name) { // нет хвостовых цифр → цельный диск
		return false
	}
	base := name[:i]
	if strings.HasSuffix(base, "p") { // nvme0n1p1 → nvme0n1
		base = base[:len(base)-1]
	}
	return base != "" && base != name && all[base]
}

// суммарные счётчики дискового I/O по цельным физическим дискам (/proc/diskstats):
// сектора (×512 = байты) прочитано/записано и число операций чтения/записи.
func readDiskIO() (rdBytes, wrBytes, rdOps, wrOps uint64) {
	f, err := os.Open("/proc/diskstats")
	if err != nil {
		return
	}
	defer f.Close()
	type row struct {
		name                   string
		rIOs, rSec, wIOs, wSec uint64
	}
	var rows []row
	names := map[string]bool{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 10 {
			continue
		}
		name := fields[2]
		names[name] = true
		rIOs, _ := strconv.ParseUint(fields[3], 10, 64) // reads completed
		rSec, _ := strconv.ParseUint(fields[5], 10, 64) // sectors read
		wIOs, _ := strconv.ParseUint(fields[7], 10, 64) // writes completed
		wSec, _ := strconv.ParseUint(fields[9], 10, 64) // sectors written
		rows = append(rows, row{name, rIOs, rSec, wIOs, wSec})
	}
	for _, r := range rows {
		if skipDiskDev(r.name) || isPartition(r.name, names) {
			continue
		}
		rdOps += r.rIOs
		wrOps += r.wIOs
		rdBytes += r.rSec * 512
		wrBytes += r.wSec * 512
	}
	return
}

type diskCtr struct {
	rIOs, wIOs, rTicks, wTicks, ioTicks uint64
}

// per-device счётчики для %util и await (/proc/diskstats), только цельные диски:
// rIOs/wIOs — завершённые операции; rTicks/wTicks — время в очереди/обслуживании (мс);
// ioTicks (поле 13) — время, когда устройство было занято (база для %util).
func readDiskDevs() map[string]diskCtr {
	m := map[string]diskCtr{}
	f, err := os.Open("/proc/diskstats")
	if err != nil {
		return m
	}
	defer f.Close()
	type raw struct {
		name string
		c    diskCtr
	}
	var rows []raw
	names := map[string]bool{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 13 {
			continue
		}
		name := fields[2]
		names[name] = true
		rIOs, _ := strconv.ParseUint(fields[3], 10, 64)
		rTicks, _ := strconv.ParseUint(fields[6], 10, 64)
		wIOs, _ := strconv.ParseUint(fields[7], 10, 64)
		wTicks, _ := strconv.ParseUint(fields[10], 10, 64)
		ioTicks, _ := strconv.ParseUint(fields[12], 10, 64)
		rows = append(rows, raw{name, diskCtr{rIOs, wIOs, rTicks, wTicks, ioTicks}})
	}
	for _, r := range rows {
		if skipDiskDev(r.name) || isPartition(r.name, names) {
			continue
		}
		m[r.name] = r.c
	}
	return m
}

// температура диска (°C) из hwmon под блочным устройством (drivetemp для SATA,
// nvme для NVMe). nil, если датчика нет (частый случай на VM без passthrough).
func readDiskTemp(dev string) *float64 {
	base := "/sys/block/" + dev + "/device/hwmon"
	ents, err := os.ReadDir(base)
	if err != nil {
		return nil
	}
	for _, e := range ents {
		if t := readMilliC(base + "/" + e.Name() + "/temp1_input"); t >= 0 {
			return &t
		}
	}
	return nil
}

// conntrack: текущее число отслеживаемых соединений и лимит таблицы (0,0 если модуль не загружен).
func readConntrack() (count, max float64) {
	rd := func(p string) float64 {
		b, err := os.ReadFile(p)
		if err != nil {
			return -1
		}
		v, _ := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
		return v
	}
	count = rd("/proc/sys/net/netfilter/nf_conntrack_count")
	max = rd("/proc/sys/net/netfilter/nf_conntrack_max")
	if max < 0 {
		max = rd("/proc/sys/net/nf_conntrack_max")
	}
	if count < 0 {
		count = 0
	}
	if max < 0 {
		max = 0
	}
	return
}

// сокеты из /proc/net/sockstat (+sockstat6): всего, TCP inuse, TCP time-wait, UDP inuse.
func readSockstat() (used, tcp, tw, udp float64) {
	pf := func(fields []string, key string) float64 {
		for i := 0; i+1 < len(fields); i++ {
			if fields[i] == key {
				v, _ := strconv.ParseFloat(fields[i+1], 64)
				return v
			}
		}
		return 0
	}
	parse := func(p string) {
		f, err := os.Open(p)
		if err != nil {
			return
		}
		defer f.Close()
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			fields := strings.Fields(sc.Text())
			if len(fields) < 2 {
				continue
			}
			switch fields[0] {
			case "sockets:":
				used += pf(fields, "used")
			case "TCP:", "TCP6:":
				tcp += pf(fields, "inuse")
				tw += pf(fields, "tw") // tw есть только в v4
			case "UDP:", "UDP6:":
				udp += pf(fields, "inuse")
			}
		}
	}
	parse("/proc/net/sockstat")
	parse("/proc/net/sockstat6")
	return
}

// USER_HZ (jiffies/сек) — на практике 100 на всех Linux x86/arm; без cgo sysconf
// недоступен. Ошибка в константе лишь мас­штабирует CPU%, порядок топа не меняет.
const clkTck = 100.0

// сколько процессов слать в каждом топе
const topProcN = 8

type procSample struct {
	ticks   uint64 // utime+stime (jiffies)
	rss     uint64 // резидентная память, байты
	comm    string
	threads int
	state   string
}

// снимок всех процессов из /proc/<pid>/stat: тики CPU (utime+stime), RSS, имя.
// comm в stat — в скобках и может содержать пробелы/скобки → берём по последней ')'.
func readProcs() map[int]procSample {
	m := map[int]procSample{}
	ents, err := os.ReadDir("/proc")
	if err != nil {
		return m
	}
	ps := uint64(os.Getpagesize())
	for _, e := range ents {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue // не числовой каталог (не PID)
		}
		b, err := os.ReadFile("/proc/" + e.Name() + "/stat")
		if err != nil {
			continue // процесс завершился между ReadDir и чтением
		}
		s := string(b)
		op := strings.IndexByte(s, '(')
		cl := strings.LastIndexByte(s, ')')
		if op < 0 || cl < 0 || cl < op || cl+2 >= len(s) {
			continue
		}
		comm := s[op+1 : cl]
		tail := strings.Fields(s[cl+2:]) // поля начиная с state (поле 3)
		if len(tail) < 22 {
			continue
		}
		state := tail[0]                                   // поле 3
		utime, _ := strconv.ParseUint(tail[11], 10, 64)    // поле 14
		stime, _ := strconv.ParseUint(tail[12], 10, 64)    // поле 15
		threads, _ := strconv.Atoi(tail[17])               // поле 20 (num_threads)
		rssPages, _ := strconv.ParseUint(tail[21], 10, 64) // поле 24 (страницы)
		m[pid] = procSample{ticks: utime + stime, rss: rssPages * ps, comm: comm, threads: threads, state: state}
	}
	return m
}

// приватная память процесса ≈ RSS − общая (RssShmem). Для postgres/redis это
// снимает двойной учёт shared_buffers, из-за которого все бэкенды показывали
// одинаковый гигантский RSS.
func privateRSS(p procStat) uint64 {
	if p.Shared >= p.RSS {
		return 0
	}
	return p.RSS - p.Shared
}

// topProcs строит два топ-N среза: по CPU% (дельта тиков за интервал) и по
// приватной памяти. Топ-CPU и кандидаты топа памяти обогащаются cmdline/владельцем/
// общей памятью (несколько десятков чтений /proc за интервал — дёшево).
// Возвращаемые срезы — независимые копии (append копирует значения).
func topProcs(prev, cur sample) (topCPU, topMem []procStat) {
	el := cur.at.Sub(prev.at).Seconds()
	if len(cur.procs) == 0 || el <= 0 {
		return nil, nil
	}
	list := make([]procStat, 0, len(cur.procs))
	for pid, c := range cur.procs {
		cpu := 0.0
		if p, ok := prev.procs[pid]; ok && c.ticks >= p.ticks {
			cpu = float64(c.ticks-p.ticks) / clkTck / el * 100
		}
		list = append(list, procStat{
			Pid: pid, Comm: c.comm, CPU: round1(cpu), RSS: c.rss,
			Threads: c.threads, State: c.state,
		})
	}
	// топ по CPU
	sort.Slice(list, func(i, j int) bool { return list[i].CPU > list[j].CPU })
	topCPU = append(topCPU, list[:min(topProcN, len(list))]...)
	for i := range topCPU {
		enrichProc(&topCPU[i])
	}
	// память: берём широкий набор кандидатов по RSS, обогащаем (узнаём общую
	// память), затем пересортировываем по приватной и берём топ-N. Любой процесс
	// с большой приватной памятью попадёт в кандидаты, т.к. private ≤ RSS.
	sort.Slice(list, func(i, j int) bool { return list[i].RSS > list[j].RSS })
	cand := list[:min(topProcN*3, len(list))]
	for i := range cand {
		enrichProc(&cand[i])
	}
	sort.Slice(cand, func(i, j int) bool { return privateRSS(cand[i]) > privateRSS(cand[j]) })
	topMem = append(topMem, cand[:min(topProcN, len(cand))]...)
	return topCPU, topMem
}

const procCmdlineMax = 140

// uidCache — ленивый кэш uid→имя из /etc/passwd (одно чтение файла).
var (
	uidCacheOnce sync.Once
	uidCache     map[uint32]string
)

func uidName(uid uint32) string {
	uidCacheOnce.Do(func() {
		uidCache = map[uint32]string{}
		b, err := os.ReadFile("/etc/passwd")
		if err != nil {
			return
		}
		for _, ln := range strings.Split(string(b), "\n") {
			f := strings.Split(ln, ":")
			if len(f) >= 3 {
				if u, err := strconv.ParseUint(f[2], 10, 32); err == nil {
					uidCache[uint32(u)] = f[0]
				}
			}
		}
	})
	if n, ok := uidCache[uid]; ok {
		return n
	}
	return strconv.FormatUint(uint64(uid), 10)
}

// enrichProc дочитывает по одному процессу то, что дорого читать для всех:
// командную строку, владельца и общую (shared) память. Всё — из world-readable
// файлов /proc, без CAP_SYS_PTRACE (агент непривилегированный).
func enrichProc(p *procStat) {
	dir := "/proc/" + strconv.Itoa(p.Pid)
	// командная строка: аргументы разделены NUL
	if b, err := os.ReadFile(dir + "/cmdline"); err == nil && len(b) > 0 {
		cl := strings.TrimRight(string(b), "\x00")
		cl = strings.TrimSpace(strings.ReplaceAll(cl, "\x00", " "))
		if r := []rune(cl); len(r) > procCmdlineMax {
			cl = string(r[:procCmdlineMax]) + "…"
		}
		p.Cmdline = cl
	}
	// общая память (RssShmem) из /proc/pid/status — читаема без ptrace
	if b, err := os.ReadFile(dir + "/status"); err == nil {
		for _, ln := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(ln, "RssShmem:") {
				fields := strings.Fields(ln)
				if len(fields) >= 2 {
					if kb, err := strconv.ParseUint(fields[1], 10, 64); err == nil {
						p.Shared = kb * 1024
					}
				}
				break
			}
		}
	}
	// владелец процесса = владелец каталога /proc/pid
	if fi, err := os.Stat(dir); err == nil {
		if st, ok := fi.Sys().(*syscall.Stat_t); ok {
			p.User = uidName(st.Uid)
		}
	}
}

func uptime() int64 {
	b, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	parts := strings.Fields(string(b))
	if len(parts) == 0 {
		return 0
	}
	v, _ := strconv.ParseFloat(parts[0], 64)
	return int64(v)
}

// Настоящие ФС с данными. vfat/exfat намеренно НЕ включены: это EFI-раздел
// (/boot/efi) и примонтированные флешки — мониторить их смысла нет.
var realFS = map[string]bool{
	"ext2": true, "ext3": true, "ext4": true, "xfs": true, "btrfs": true,
	"zfs": true, "f2fs": true, "reiserfs": true, "jfs": true, "ntfs": true,
}

func disks() []disk {
	f, err := os.Open("/proc/mounts")
	if err != nil {
		return nil
	}
	defer f.Close()
	// дедуп по УСТРОЙСТВУ ФС, а не по пути: bind-mount (напр. systemd ReadWritePaths
	// /opt/kervax/bin) — та же ФС, что и /, и не должен показываться отдельным диском.
	seenDev := map[uint64]bool{}
	var out []disk
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 3 {
			continue
		}
		mount, fstype := fields[1], fields[2]
		if !realFS[fstype] {
			continue
		}
		var meta syscall.Stat_t
		if syscall.Stat(mount, &meta) != nil || seenDev[meta.Dev] {
			continue // недоступно или эта ФС уже учтена (bind-mount / повторный маунт)
		}
		var st syscall.Statfs_t
		if syscall.Statfs(mount, &st) != nil || st.Blocks == 0 {
			continue
		}
		bs := uint64(st.Bsize)
		total := st.Blocks * bs
		if total < minDiskBytes {
			continue // мелкая системщина (/boot и т.п.) — пропускаем
		}
		seenDev[meta.Dev] = true
		used := (st.Blocks - st.Bfree) * bs
		out = append(out, disk{Mount: mount, Used: used, Total: total})
	}
	return out
}

// статичные атрибуты хоста — считаем один раз на старте
var hostCPUModel, hostVirt string
var hostIsVM bool

func cpuModel() string {
	f, err := os.Open("/proc/cpuinfo")
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if strings.HasPrefix(sc.Text(), "model name") {
			if _, v, ok := strings.Cut(sc.Text(), ":"); ok {
				return strings.TrimSpace(v)
			}
		}
	}
	return ""
}

// detectVirt — VM ли это и какой гипервизор (по hypervisor-флагу + DMI, без утилит).
func detectVirt() (bool, string) {
	isVM := false
	if b, err := os.ReadFile("/proc/cpuinfo"); err == nil && strings.Contains(string(b), "hypervisor") {
		isVM = true
	}
	read := func(p string) string {
		b, _ := os.ReadFile("/sys/class/dmi/id/" + p)
		return strings.TrimSpace(string(b))
	}
	bios := read("bios_vendor")
	c := strings.ToLower(read("sys_vendor") + " " + read("product_name") + " " + bios)
	name := ""
	switch {
	case strings.Contains(c, "microsoft") && strings.Contains(c, "virtual"):
		name = "Hyper-V"
	case strings.Contains(c, "vmware"):
		name = "VMware"
	case strings.Contains(c, "virtualbox"), strings.Contains(c, "innotek"):
		name = "VirtualBox"
	case strings.Contains(c, "xen"):
		name = "Xen"
	case strings.Contains(c, "amazon"), strings.Contains(c, "ec2"):
		name = "Amazon EC2"
	case strings.Contains(c, "google"):
		name = "Google Cloud"
	case strings.Contains(c, "digitalocean"):
		name = "DigitalOcean"
	case strings.Contains(c, "openstack"):
		name = "OpenStack"
	case strings.Contains(c, "kvm"):
		name = "KVM"
	case strings.Contains(c, "qemu"), strings.Contains(bios, "SeaBIOS"):
		name = "QEMU"
	}
	if name != "" {
		isVM = true
	} else if isVM {
		name = "Virtual Machine"
	}
	return isVM, name
}

func osName() string {
	f, err := os.Open("/etc/os-release")
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if strings.HasPrefix(sc.Text(), "PRETTY_NAME=") {
			return strings.Trim(strings.TrimPrefix(sc.Text(), "PRETTY_NAME="), `"`)
		}
	}
	return ""
}

type sample struct {
	cpu                  cpuStat
	cores                []cpuStat // per-core счётчики
	rx, tx               uint64
	rdB, wrB, rdIO, wrIO uint64             // накопительные счётчики дискового I/O
	swpin, swpout        uint64             // накопительные счётчики страниц swap in/out
	nifs                 map[string]netCtr  // per-interface счётчики rx/tx
	ddevs                map[string]diskCtr // per-device счётчики диска
	procs                map[int]procSample // снимок процессов (для дельты CPU)
	thr                  uint64             // накопительный счётчик троттлингов
	thrOK                bool               // есть ли счётчик троттлинга (на VM нет)
	oom                  uint64             // накопительный счётчик OOM-киллов (/proc/vmstat oom_kill)
	oomOK                bool               // есть ли счётчик (ядро ≥4.13)
	at                   time.Time
}

func snap() sample {
	rx, tx := readNet()
	rdB, wrB, rdIO, wrIO := readDiskIO()
	agg, cores := readCPUAll()
	thr, thrOK := readThrottleCount()
	swpin, swpout, oom, oomOK := readVmstat()
	return sample{
		cpu: agg, cores: cores, rx: rx, tx: tx,
		rdB: rdB, wrB: wrB, rdIO: rdIO, wrIO: wrIO,
		swpin: swpin, swpout: swpout,
		nifs: readNetIfaces(), ddevs: readDiskDevs(), procs: readProcs(),
		thr: thr, thrOK: thrOK, oom: oom, oomOK: oomOK, at: time.Now(),
	}
}

func collect(prev sample) (report, sample) {
	cur := snap()
	// разбивка CPU по дельте jiffies
	var cpuBusy, cUser, cSys, cIo, cIrq float64
	if dt := float64(cur.cpu.total - prev.cpu.total); dt > 0 {
		p := func(a, b uint64) float64 {
			if a < b {
				return 0
			}
			return float64(a-b) / dt * 100
		}
		cUser = p(cur.cpu.user, prev.cpu.user)
		cSys = p(cur.cpu.system, prev.cpu.system)
		cIo = p(cur.cpu.iowait, prev.cpu.iowait)
		cIrq = p(cur.cpu.irq, prev.cpu.irq)
		cpuBusy = 100 - p(cur.cpu.idle, prev.cpu.idle)
	}
	netRx, netTx := 0.0, 0.0
	dRead, dWrite, dRdIO, dWrIO := 0.0, 0.0, 0.0, 0.0
	swapIn, swapOut := 0.0, 0.0
	var netIfaces []ifaceRate
	var diskDevs []devRate
	if el := cur.at.Sub(prev.at).Seconds(); el > 0 {
		rate := func(a, b uint64) float64 {
			if a < b {
				return 0 // счётчик сбросился (ребут) — пропускаем
			}
			return float64(a-b) / el
		}
		netRx, netTx = rate(cur.rx, prev.rx), rate(cur.tx, prev.tx)
		dRead, dWrite = rate(cur.rdB, prev.rdB), rate(cur.wrB, prev.wrB)
		dRdIO, dWrIO = rate(cur.rdIO, prev.rdIO), rate(cur.wrIO, prev.wrIO)
		ps := float64(os.Getpagesize())
		swapIn, swapOut = rate(cur.swpin, prev.swpin)*ps, rate(cur.swpout, prev.swpout)*ps
		// per-interface rx/tx (байт/сек) + ошибки/дропы (пакетов/сек)
		for name, c := range cur.nifs {
			if p, ok := prev.nifs[name]; ok {
				netIfaces = append(netIfaces, ifaceRate{
					name, rate(c.rx, p.rx), rate(c.tx, p.tx),
					rate(c.errs, p.errs), rate(c.drops, p.drops),
				})
			}
		}
		sort.Slice(netIfaces, func(i, j int) bool { return netIfaces[i].If < netIfaces[j].If })
		// per-device %util (доля времени занятости) и await (мс/операция)
		elMs := el * 1000
		for name, c := range cur.ddevs {
			p, ok := prev.ddevs[name]
			if !ok {
				continue
			}
			util := 0.0
			if c.ioTicks >= p.ioTicks {
				util = float64(c.ioTicks-p.ioTicks) / elMs * 100
			}
			if util > 100 {
				util = 100 // многоочередные NVMe могут дать >100 — клампим
			}
			await := 0.0
			if di := int64(c.rIOs+c.wIOs) - int64(p.rIOs+p.wIOs); di > 0 {
				if dt := int64(c.rTicks+c.wTicks) - int64(p.rTicks+p.wTicks); dt > 0 {
					await = float64(dt) / float64(di)
				}
			}
			diskDevs = append(diskDevs, devRate{name, round1(util), round1(await), readDiskTemp(name)})
		}
		sort.Slice(diskDevs, func(i, j int) bool { return diskDevs[i].Dev < diskDevs[j].Dev })
	}
	// топ процессов: CPU% по дельте тиков, RSS — мгновенный
	topCPU, topMem := topProcs(prev, cur)
	// conntrack + сокеты — мгновенные значения
	ctCount, ctMax := readConntrack()
	sockUsed, sockTCP, sockTW, sockUDP := readSockstat()
	// per-core загрузка (%)
	var corePct []float64
	if n := len(cur.cores); n > 0 && len(prev.cores) == n {
		corePct = make([]float64, n)
		for i := range cur.cores {
			corePct[i] = round1(busyPct(cur.cores[i], prev.cores[i]))
		}
	}
	// частота/температура — мгновенные; nil (JSON null) если датчика нет (VM)
	var freq, temp, throttle *float64
	if v, ok := readFreqMHz(); ok {
		freq = &v
	}
	if v, ok := readTempC(); ok {
		temp = &v
	}
	// троттлинг — дельта накопительного счётчика (событий за интервал); nil если нет счётчика
	if cur.thrOK && prev.thrOK {
		d := 0.0
		if cur.thr >= prev.thr {
			d = float64(cur.thr - prev.thr)
		}
		throttle = &d
	}
	// OOM-киллы — дельта накопительного счётчика за интервал (счётчик сбрасывается
	// при ребуте → берём дельту только если не «уехал» вниз); nil если счётчика нет
	var oomKill *float64
	oomVictim := ""
	if cur.oomOK && prev.oomOK {
		d := 0.0
		if cur.oom >= prev.oom {
			d = float64(cur.oom - prev.oom)
		}
		oomKill = &d
		if d > 0 { // случился килл → пробуем узнать жертву из kmsg (best-effort)
			oomVictim = readOOMVictim()
		}
	}
	mi := meminfo()
	host, _ := os.Hostname()
	memTotal := mi["MemTotal"]
	memUsed := memTotal - mi["MemAvailable"]
	dk := collectDocker() // один раз: нужен и для Docker, и для детекта бэкап-сервера
	r := report{
		Hostname: host, OS: osName(), AgentVersion: version, LocalIP: localIP(),
		CPUModel: hostCPUModel, IsVM: hostIsVM, Virt: hostVirt,
		Uptime: uptime(), CPUPercent: cpuBusy,
		MemUsed: memUsed, MemTotal: memTotal,
		SwapUsed: mi["SwapTotal"] - mi["SwapFree"], SwapTotal: mi["SwapTotal"],
		Load: loadavg(), Disks: disks(), DBEngines: collectDBEngines(),
		NetRx: netRx, NetTx: netTx,
		DiskRead: dRead, DiskWrite: dWrite,
		DiskReadIOPS: dRdIO, DiskWriteIOPS: dWrIO,
		CPUCores: runtime.NumCPU(),
		CPUUser:  cUser, CPUSystem: cSys, CPUIowait: cIo, CPUIrq: cIrq,
		CPUCoresPct: corePct, CPUFreqMHz: freq, CPUTemp: temp, CPUThrottle: throttle,
		OOMKill: oomKill, OOMVictim: oomVictim,
		MemCached: mi["Buffers"] + mi["Cached"], MemFree: mi["MemFree"],
		SwapIn: swapIn, SwapOut: swapOut,
		MemSlab: mi["Slab"], MemDirty: mi["Dirty"], MemWriteback: mi["Writeback"],
		NetIfaces: netIfaces, DiskDevs: diskDevs,
		TopCPU: topCPU, TopMem: topMem,
		ConntrackCount: ctCount, ConntrackMax: ctMax,
		SockUsed: sockUsed, SockTCP: sockTCP, SockTW: sockTW, SockUDP: sockUDP,
		// watchdog: systemd задаёт WATCHDOG_USEC, когда в юните есть WatchdogSec (Type=notify).
		// Нет → юнит старый, systemd не поднимет зависший агент → панель советует переустановку.
		Caps:          map[string]bool{"kmsg": canReadKmsg(), "proc_full": procFullyVisible(), "watchdog": os.Getenv("WATCHDOG_USEC") != ""},
		Docker:        dk,
		Backup:        collectBackup(),
		BackupServer:  collectBackupServer(dk),
		SetupVersions: collectSetupVersions(),
		Clock:         collectClock(),
	}
	// kube собираем в переменную: он же нужен для скрейпа сервисов (podIP есть только там)
	r.Kube = collectKube()
	r.Services = collectServices(dk, r.Kube)
	r.WebServices = collectWebServices(r.Kube)
	r.DBStats = collectDBStats()
	return r, cur
}

// collectClock — статус синхронизации времени через timedatectl (read-only, unprivileged).
// Оффсет от демона не читаем: реальный сдвиг меряет панель по clock_unix. Здесь — синхрон/
// демон, чтобы отличить «часы уехали, но демон есть» от «синхронизировать нечем».
func collectClock() *clockInfo {
	// только через cmdOut: голый exec.Command отдаёт детям NOTIFY_SOCKET, и systemd-утилиты
	// пишут в него, а он при NotifyAccess=main их отвергает — журнал забивался строками
	// «Got notification message from PID …» (7 штук за цикл: timedatectl + шесть is-active)
	out := cmdOut(5*time.Second, "timedatectl", "show")
	if out == "" {
		return nil // не systemd/нет timedatectl — блок не шлём
	}
	ci := &clockInfo{}
	for _, ln := range strings.Split(out, "\n") {
		k, v, ok := strings.Cut(ln, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(k) {
		case "NTP":
			ci.NTP = v == "yes"
		case "NTPSynchronized":
			ci.Synced = v == "yes"
		}
	}
	// активный демон времени — для диагноза «синхронизировать нечем» (пусто = ни один не жив)
	for _, svc := range []string{"systemd-timesyncd", "chronyd", "chrony", "ntpd", "ntpsec", "openntpd"} {
		if cmdOut(5*time.Second, "systemctl", "is-active", svc) == "active" {
			ci.Service = svc
			break
		}
	}
	return ci
}

// collectSetupVersions — версии установленных setup-скриптов из /var/lib/kervax/versions/*.ver.
// Панель сверяет с текущими (раздаваемыми) и флагует устаревшие helper'ы для ручного re-install.
// Версия — строка «мажор.минор» (0.12). Раньше парсили в int; с точкой Atoi падал бы,
// версия не попадала в отчёт, и панель считала helper устаревшим навсегда. Строку не
// разбираем вовсе: сравнивать — дело панели, агент лишь докладывает, что установлено.
func collectSetupVersions() map[string]string {
	out := map[string]string{}
	ents, err := os.ReadDir("/var/lib/kervax/versions")
	if err != nil {
		return out
	}
	for _, e := range ents {
		name, ok := strings.CutSuffix(e.Name(), ".ver")
		if !ok {
			continue
		}
		if b, err := os.ReadFile("/var/lib/kervax/versions/" + e.Name()); err == nil {
			if v := strings.TrimSpace(string(b)); v != "" && len(v) <= 16 {
				out[name] = v
			}
		}
	}
	return out
}

// canReadKmsg — доступен ли /dev/kmsg (нужен CAP_SYSLOG при dmesg_restrict=1).
// Без него не узнать имя OOM-жертвы. Флаг едет в отчёт → панель подскажет фикс.
func canReadKmsg() bool {
	f, err := os.OpenFile("/dev/kmsg", os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return false
	}
	_ = f.Close()
	return true
}

// procFullyVisible — видит ли непривилегированный агент ЧУЖИЕ процессы. При hidepid на
// /proc (mount -o hidepid=1|2) он видит только свои — и /proc-скан СУБД пропустил бы
// нативно установленные базы других пользователей (в контейнерах видны через docker/kube
// по образам, а нативные — нет). /proc/1 (init) всегда root: если мы, будучи kervax, не
// можем прочитать его comm — hidepid ограничивает нас. Флаг едет в отчёт → панель честно
// предупреждает, что детект СУБД может быть неполным.
func procFullyVisible() bool {
	if os.Geteuid() == 0 {
		return true // root видит всё независимо от hidepid
	}
	_, err := os.ReadFile("/proc/1/comm")
	return err == nil
}

// --- конфиг: url= / token= из файла ---

// где агент читает Docker Engine API. По умолчанию — родной сокет (нужен доступ:
// docker-группа ИЛИ, безопаснее, read-only docker-socket-proxy, отдающий сюда сокет).
// Переопределяется docker_host= в конфиге (напр. tcp://127.0.0.1:2375 у proxy).
var dockerHost = "/var/run/docker.sock"

// путь к kube.json (server/ca/token выделенного SA, см. kube-setup.sh). Читаем на
// чтение под непривилегированным kervax. Переопределяется kube_config= в конфиге.
var kubeConfigPath = "/etc/kervax/kube.json"

// dockerHTTP — http-клиент к Engine API поверх unix-сокета или tcp (для proxy).
// ОДИН транспорт на процесс. Раньше dockerHTTP создавал новый http.Transport на КАЖДЫЙ
// вызов, а collectDocker зовёт его каждые 15с. Транспорт держит пул keep-alive соединений
// к docker-сокету; после выхода из функции он становится недостижим, но его соединение
// висит до сборки мусора — и при частых опросах FD/ESTABLISHED растут неограниченно
// (ловили 4900+ FD и 14 000+ соединений до перезапуска). Общий транспорт возвращает
// соединение в пул через Body.Close() и переиспользует его, поэтому пул не растёт.
// dockerHost читаем в момент дайла (а не кэшируем): он может смениться при чтении конфига.
var dockerTransport = &http.Transport{
	DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		network, addr := "unix", dockerHost
		if strings.HasPrefix(dockerHost, "tcp://") {
			network, addr = "tcp", strings.TrimPrefix(dockerHost, "tcp://")
		}
		return (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, network, addr)
	},
	MaxIdleConns:        4,
	MaxIdleConnsPerHost: 4,
	IdleConnTimeout:     60 * time.Second, // простаивающее соединение закрываем, а не держим вечно
}

// dockerHTTP — клиент с нужным таймаутом поверх ОБЩЕГО транспорта. Сам Client дёшев
// (в отличие от Transport, который держит пул) — его можно создавать на каждый вызов.
func dockerHTTP(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, Transport: dockerTransport}
}

func dockerGet(cl *http.Client, path string, out any) error {
	resp, err := cl.Get("http://docker" + path)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("docker api %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// dockerPresent — установлен ли Docker (без прав): сокет существует / dockerd в /proc.
func dockerPresent() bool {
	if strings.HasPrefix(dockerHost, "tcp://") {
		return true // указан proxy — считаем, что докер есть
	}
	if _, err := os.Stat(dockerHost); err == nil {
		return true
	}
	// fallback: ищем dockerd среди процессов (сокет мог быть в другом месте)
	ents, _ := os.ReadDir("/proc")
	for _, e := range ents {
		if _, err := strconv.Atoi(e.Name()); err != nil {
			continue
		}
		if b, err := os.ReadFile("/proc/" + e.Name() + "/comm"); err == nil {
			if strings.TrimSpace(string(b)) == "dockerd" {
				return true
			}
		}
	}
	return false
}

// Движки, которые НЕЛЬЗЯ надёжно забэкапить копированием файлов на живую: нужен дамп
// (pg_dump/mysqldump/mongodump/BACKUP TABLE) или снапшот средствами самой СУБД.
// Ключ — имя процесса (comm, максимум 15 символов в ядре!), значение — движок для панели.
var dbProcSignatures = map[string]string{
	"postgres": "PostgreSQL", "postmaster": "PostgreSQL",
	"mysqld": "MySQL/MariaDB", "mariadbd": "MySQL/MariaDB",
	"mongod":          "MongoDB",
	"clickhouse-serv": "ClickHouse", // comm обрезан ядром до 15 символов
	"redis-server":    "Redis", "valkey-server": "Valkey",
	"keydb-server": "Redis", "dragonfly": "Redis", // redis-совместимые форки
	"influxd": "InfluxDB", "victoria-metric": "VictoriaMetrics",
	"etcd": "etcd", "cockroach": "CockroachDB",
	"scylla": "ScyllaDB", "arangod": "ArangoDB", "rethinkdb": "RethinkDB",
	"memcached": "Memcached", "surreal": "SurrealDB",
	"sqlservr": "MS SQL Server", "prometheus": "Prometheus", "minio": "MinIO",
	"vault": "HashiCorp Vault", "consul": "Consul",
	"grafana": "Grafana", "grafana-server": "Grafana",
}

// Движки на JVM/BEAM: comm у всех одинаковый (java / beam.smp), различить можно только
// по cmdline. Читаем его ТОЛЬКО для этих двух comm — иначе скан подорожал бы на всю
// таблицу процессов ради горстки движков.
var dbVMComms = map[string]bool{"java": true, "beam.smp": true}
var dbCmdlineSignatures = []struct{ needle, engine string }{
	{"elasticsearch", "Elasticsearch"},
	{"opensearch", "Elasticsearch"},
	{"org.apache.cassandra", "Cassandra"},
	{"org.apache.zookeeper", "ZooKeeper"},
	{"kafka.kafka", "Kafka"}, // иглы ТОЛЬКО в нижнем регистре: cmdline сравниваем lower
	{"neo4j", "Neo4j"},
	{"rabbitmq", "RabbitMQ"},
	{"couchdb", "CouchDB"},
}

// Запасной матч по префиксу: ядро режет comm до 15 символов, и точное имя зависит от
// сборки (clickhouse-server/clickhouse-serv/clickhouse). Проверять на всех вариантах
// нечем, поэтому ловим по началу имени.
var dbProcPrefixes = []struct{ prefix, engine string }{
	{"clickhouse", "ClickHouse"},
	{"mariadb", "MySQL/MariaDB"},
	{"cockroach", "CockroachDB"},
	{"victoria-met", "VictoriaMetrics"},
}

// webService — веб-сервер/прокси на ноде. Sites (домены) наполняются позже: из k8s Ingress
// (read-only kube) и из хостового nginx (root-хелпер `nginx -T` → спул). Детект — тот же
// скан /proc, поэтому ловит и нативные, и в docker, и в подах.
type webService struct {
	Kind   string   `json:"kind"`
	Source string   `json:"source,omitempty"`
	Sites  []string `json:"sites,omitempty"`
}

// веб-серверы/прокси по comm. ingress-nginx ловим префиксом (comm обрезан ядром до 15:
// «nginx-ingress-controller» → «nginx-ingress-c»); его nginx-воркеры отдельным «nginx» не двоим.
var webProcSignatures = map[string]string{
	"nginx": "nginx", "envoy": "Envoy", "haproxy": "HAProxy",
	"caddy": "Caddy", "httpd": "Apache", "apache2": "Apache", "traefik": "Traefik",
}
var webProcPrefixes = []struct{ prefix, kind string }{
	{"nginx-ingress", "ingress-nginx"},
}

// collectWebServices — какие веб-серверы/прокси работают на ноде (по скану /proc).
// kube (может быть nil) даёт домены из Ingress → вешаем их на ingress-контроллер.
func collectWebServices(kube *kubeInfo) []webService {
	seen := map[string]bool{}
	ents, _ := os.ReadDir("/proc")
	for _, e := range ents {
		if _, err := strconv.Atoi(e.Name()); err != nil {
			continue
		}
		b, err := os.ReadFile("/proc/" + e.Name() + "/comm")
		if err != nil {
			continue
		}
		comm := strings.TrimSpace(string(b))
		matched := false
		for _, p := range webProcPrefixes {
			if strings.HasPrefix(comm, p.prefix) {
				seen[p.kind] = true
				matched = true
				break
			}
		}
		if matched {
			continue
		}
		if k, ok := webProcSignatures[comm]; ok {
			seen[k] = true
		}
	}
	// nginx-воркеры ingress-контроллера не показываем ещё и как отдельный «nginx»
	if seen["ingress-nginx"] {
		delete(seen, "nginx")
	}
	out := make([]webService, 0, len(seen))
	for k := range seen {
		out = append(out, webService{Kind: k})
	}
	// домены из k8s Ingress → на ingress-контроллер (или отдельной записью, если процесс
	// контроллера не пойман, но Ingress'ы в кластере есть)
	if kube != nil && len(kube.ingressHosts) > 0 {
		attached := false
		for i := range out {
			// контроллер маршрутизации кластера: ingress-nginx / Traefik / Envoy (Gateway API)
			if out[i].Kind == "ingress-nginx" || out[i].Kind == "Traefik" || out[i].Kind == "Envoy" {
				out[i].Source = "kubernetes"
				out[i].Sites = kube.ingressHosts
				attached = true
			}
		}
		if !attached {
			out = append(out, webService{Kind: "ingress-nginx", Source: "kubernetes", Sites: kube.ingressHosts})
		}
	}
	// домены хостового/контейнерного nginx/apache: root-хелпер webserver-setup дампит
	// server_name в файл (агент неприв. — сам конфиги не читает), тут только ЧИТАЕМ и мержим
	if hs := hostWebSites(); len(hs) > 0 {
		for i := range out {
			if sites := hs[out[i].Kind]; len(sites) > 0 {
				out[i].Sites = mergeSites(out[i].Sites, sites)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Kind < out[j].Kind })
	return out
}

// hostWebSites — домены хостового веб-сервера из /var/lib/kervax/web-sites.json (его пишет
// root-хелпер webserver-setup по таймеру). Нет файла/битый → nil, не ломаемся.
// Инвентарь СУБД (базы/размеры/логины/версия). Собирает root-хелпер dbstat-setup:
// агент неприв., `docker exec` ему запрещён агентской проксёй — сам он в базу не сходит.
// Здесь ТОЛЬКО читаем его файл. Паролей в нём нет by design.
type dbEntry struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
}

type dbStat struct {
	Engine    string    `json:"engine"`
	Container string    `json:"container,omitempty"`
	Version   string    `json:"version,omitempty"`
	DBs       []dbEntry `json:"dbs,omitempty"`
	Users     []string  `json:"users,omitempty"`
}

func collectDBStats() []dbStat {
	b, err := os.ReadFile("/var/lib/kervax/db-stats.json")
	if err != nil {
		return nil
	}
	var f struct {
		Items []dbStat `json:"items"`
	}
	if json.Unmarshal(b, &f) != nil {
		return nil
	}
	return f.Items
}

func hostWebSites() map[string][]string {
	b, err := os.ReadFile("/var/lib/kervax/web-sites.json")
	if err != nil {
		return nil
	}
	var m map[string][]string
	if json.Unmarshal(b, &m) != nil {
		return nil
	}
	return m
}

// mergeSites — объединяет два списка доменов без дублей, сортирует.
func mergeSites(a, b []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(a)+len(b))
	for _, s := range a {
		if s != "" && !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	for _, s := range b {
		if s != "" && !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	sort.Strings(out)
	return out
}

// collectDBEngines — какие СУБД РЕАЛЬНО работают на ноде. Скан /proc ловит все сразу:
// нативно установленные, в docker-контейнерах и в подах kubernetes (их процессы тоже
// видны с хоста). Детект по образам контейнеров такого не даёт: нативные установки мимо,
// а у подов панель вообще не знает образов.
func collectDBEngines() []string {
	seen := map[string]bool{}
	ents, _ := os.ReadDir("/proc")
	for _, e := range ents {
		if _, err := strconv.Atoi(e.Name()); err != nil {
			continue
		}
		b, err := os.ReadFile("/proc/" + e.Name() + "/comm")
		if err != nil {
			continue // процесс умер между ReadDir и чтением — норма
		}
		comm := strings.TrimSpace(string(b))
		if eng, ok := dbProcSignatures[comm]; ok {
			seen[eng] = true
			continue
		}
		matched := false
		for _, p := range dbProcPrefixes {
			if strings.HasPrefix(comm, p.prefix) {
				seen[p.engine] = true
				matched = true
				break
			}
		}
		if matched || !dbVMComms[comm] {
			continue
		}
		// java/beam.smp — смотрим cmdline (в нём NUL вместо пробелов, для поиска не важно)
		cl, err := os.ReadFile("/proc/" + e.Name() + "/cmdline")
		if err != nil {
			continue
		}
		low := strings.ToLower(string(cl))
		for _, sig := range dbCmdlineSignatures {
			if strings.Contains(low, sig.needle) {
				seen[sig.engine] = true
				break
			}
		}
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// dockerBinary — абсолютный путь к docker (у systemd-юнита PATH может быть пуст,
// поэтому не полагаемся на LookPath).
func dockerBinary() string {
	for _, p := range []string{"/usr/bin/docker", "/usr/local/bin/docker", "/bin/docker"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	if p, err := exec.LookPath("docker"); err == nil {
		return p
	}
	return ""
}

// dockerExec — запуск docker-подкоманды. HOME=/tmp: под ProtectHome=yes родной
// $HOME скрыт, а docker CLI пытается читать ~/.docker → иначе падает. Без демона.
func dockerExec(bin string, args ...string) string {
	if bin == "" {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Env = append(cleanEnv(), "HOME=/tmp") // без NOTIFY_SOCKET — см. cleanEnv
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// dockerCLIVersion — версия клиента docker (без демона): «Docker version 28.5.0, …».
func dockerCLIVersion(bin string) string {
	s := dockerExec(bin, "version", "--format", "{{.Client.Version}}")
	if s != "" {
		return s
	}
	// фолбэк: парсим «Docker version X, build …»
	raw := dockerExec(bin, "--version")
	if i := strings.Index(raw, "version "); i >= 0 {
		rest := raw[i+len("version "):]
		if j := strings.IndexAny(rest, ", "); j >= 0 {
			return rest[:j]
		}
	}
	return ""
}

// collectDocker — read-only снимок Docker: наличие, версии, контейнеры (all, чтобы
// видеть и упавшие). nil = докера нет (секции в панели не будет). Present без Access
// = докер есть, но агент не видит сокет → панель подскажет безопасную настройку.
func collectDocker() *dockerInfo {
	if !dockerPresent() {
		return nil
	}
	bin := dockerBinary()
	// версии — через CLI (без демона), поэтому видны даже без доступа к сокету
	di := &dockerInfo{Present: true, Version: dockerCLIVersion(bin), Compose: dockerExec(bin, "compose", "version", "--short")}
	cl := dockerHTTP(3 * time.Second)
	var ver struct {
		Version    string
		APIVersion string `json:"ApiVersion"`
	}
	if err := dockerGet(cl, "/version", &ver); err != nil {
		return di // present, но доступа к сокету нет (версии CLI уже проставлены)
	}
	di.Access = true
	di.APIVersion = ver.APIVersion
	if ver.Version != "" {
		di.Version = ver.Version // серверная версия точнее клиентской
	}

	var raw []struct {
		Id     string
		Names  []string
		Image  string
		State  string
		Status string
	}
	if err := dockerGet(cl, "/containers/json?all=1", &raw); err == nil {
		for _, c := range raw {
			name := ""
			if len(c.Names) > 0 {
				name = strings.TrimPrefix(c.Names[0], "/")
			}
			dc := dockerContainer{Name: name, Image: c.Image, State: c.State, Status: c.Status}
			// inspect по id: RestartCount (crash-loop), restart-policy (намеренная ли
			// остановка — бэкенд не алертит down у policy=no) и health. Ошибка inspect
			// не критична — секция всё равно уедет с базовой инфой из списка.
			dockerInspect(cl, c.Id, &dc)
			di.Containers = append(di.Containers, dc)
		}
	}
	return di
}

// dockerInspect дополняет контейнер полями из /containers/{id}/json (RestartCount,
// restart-policy, health). Read-only, разрешён proxy-allowlist'ом. Тихо игнорирует
// ошибку: детект просто деградирует до данных из списка контейнеров.
func dockerInspect(cl *http.Client, id string, dc *dockerContainer) {
	if id == "" {
		return
	}
	var ins struct {
		RestartCount int
		State        struct {
			Health *struct{ Status string }
		}
		HostConfig struct {
			RestartPolicy struct{ Name string }
		}
		Mounts []struct {
			Type   string
			Source string
		}
		NetworkSettings struct {
			Networks map[string]struct{ IPAddress string }
		}
	}
	if err := dockerGet(cl, "/containers/"+url.PathEscape(id)+"/json", &ins); err != nil {
		return
	}
	dc.Restarts = ins.RestartCount
	dc.Policy = ins.HostConfig.RestartPolicy.Name
	if ins.State.Health != nil {
		dc.Health = ins.State.Health.Status
	}
	// bind-mount'ы = хост-пути, которые кто-то ОСОЗНАННО прокинул в контейнер, т.е. почти
	// наверняка данные. Панель сверит их с покрытием бэкапа. Named volumes не шлём: они
	// лежат в /var/lib/docker/volumes и попадают в бэкап по умолчанию.
	for _, m := range ins.Mounts {
		if m.Type == "bind" && strings.HasPrefix(m.Source, "/") {
			dc.Binds = append(dc.Binds, m.Source)
		}
	}
	// IP контейнера — только для скрейпа прикладных метрик (RabbitMQ и т.п.), в отчёт не уходит
	for _, n := range ins.NetworkSettings.Networks {
		if n.IPAddress != "" {
			dc.ip = n.IPAddress
			break
		}
	}
}

// runDockerCommand исполняет docker-действие из очереди панели ЧЕРЕЗ read-only proxy
// (restart/stop/start или logs) и постит результат обратно. Запускается в отдельной
// горутине — не блокирует цикл отчётов и вотчдог.
func runDockerCommand(panelURL, token string, cmd dockerCommand) {
	cl := dockerHTTP(30 * time.Second) // restart с graceful-stop может идти >10с
	ok, output := false, ""
	cpath := "/containers/" + url.PathEscape(cmd.Container)
	switch cmd.Action {
	case "restart", "stop", "start":
		resp, err := cl.Post("http://docker"+cpath+"/"+cmd.Action, "", nil)
		if err != nil {
			output = err.Error()
		} else {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			// 204 — сделано; 304 — уже в нужном состоянии (тоже успех)
			if resp.StatusCode == 204 || resp.StatusCode == 304 {
				ok, output = true, "OK"
			} else {
				output = fmt.Sprintf("docker вернул %d", resp.StatusCode)
			}
		}
	case "logs":
		base := "stdout=1&stderr=1&timestamps=1"
		if cmd.Since > 0 { // логи за N секунд: since НЕ залипает на busy-контейнерах
			output, ok = fetchDockerLogs(cpath, fmt.Sprintf("%s&since=%d", base, time.Now().Unix()-int64(cmd.Since)), 30*time.Second)
		} else {
			tail := cmd.Tail
			if tail <= 0 || tail > 20000 {
				tail = 400
			}
			// tail=N залипает на высоконагруженных контейнерах (баг docker json-file):
			// короткий таймаут, при зависании — фолбэк на since=1ч (since быстрый).
			output, ok = fetchDockerLogs(cpath, fmt.Sprintf("%s&tail=%d", base, tail), 6*time.Second)
			if !ok {
				output, ok = fetchDockerLogs(cpath, fmt.Sprintf("%s&since=%d", base, time.Now().Unix()-3600), 30*time.Second)
			}
		}
	default:
		output = "неизвестное действие"
	}
	postDockerResult(panelURL, token, cmd.ID, ok, output)
}

// fetchDockerLogs тянет логи по готовому query. Возвращает (текст, ok). ok=false —
// ошибка/не-200/таймаут (в т.ч. залипание tail на busy-контейнере) → повод к фолбэку.
func fetchDockerLogs(cpath, query string, timeout time.Duration) (string, bool) {
	resp, err := dockerHTTP(timeout).Get("http://docker" + cpath + "/logs?" + query)
	if err != nil {
		return "", false
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Sprintf("logs %d", resp.StatusCode), false
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 20_000_000)) // кап ~20МБ (для скачивания)
	if err != nil {
		return "", false // таймаут при чтении (залип) → фолбэк
	}
	return demuxDockerLogs(raw), true
}

// demuxDockerLogs убирает 8-байтовые заголовки мультиплекс-фрейминга docker
// (stdout/stderr в одном потоке). TTY-контейнеры отдают сырой поток — тогда как есть.
func demuxDockerLogs(b []byte) string {
	framed := len(b) >= 8 && b[0] <= 2 && b[1] == 0 && b[2] == 0 && b[3] == 0
	if !framed {
		return string(b)
	}
	var out bytes.Buffer
	for len(b) >= 8 {
		size := int(binary.BigEndian.Uint32(b[4:8]))
		b = b[8:]
		if size > len(b) {
			size = len(b)
		}
		out.Write(b[:size])
		b = b[size:]
	}
	return out.String()
}

// commandLoop опрашивает очередь docker-команд ЧАЩЕ, чем идёт отчёт метрик (каждые
// ~3с), чтобы restart/logs исполнялись почти сразу, а не за интервал отчёта (до 15с).
// Тело ответа крошечное (обычно пусто) — на нагрузку панели влияет незначительно.
func commandLoop(panelURL, token string) {
	tr := panelTransport(true)
	cl := &http.Client{Timeout: 10 * time.Second, Transport: tr}
	endpoint := strings.TrimRight(panelURL, "/") + "/api/agent/commands"
	// Соединение переиспользуем (опрос ежесекундный, рукопожатие на каждый — дорого),
	// но принудительно роняем раз в _pollRecycle запросов. Иначе за минуту в одном
	// соединении набегает под 20 КБ одних заголовков — ровно тот счётчик, по которому
	// DPI глушит канал (см. panelTransport). Так на соединение приходится ~9 КБ.
	const pollRecycle = 30
	polls := 0
	for {
		time.Sleep(time.Second) // быстрый опрос → restart/logs почти сразу (~1с)
		if polls++; polls >= pollRecycle {
			polls = 0
			tr.CloseIdleConnections()
		}
		req, err := http.NewRequest("GET", endpoint, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Authorization", "Bearer "+token)
		resp, err := cl.Do(req)
		if err != nil {
			continue
		}
		var out struct {
			DockerCommands []dockerCommand `json:"docker_commands"`
			KubeCommands   []kubeCommand   `json:"kube_commands"`
			BackupCommands []backupCommand `json:"backup_commands"`
		}
		if resp.StatusCode == 200 {
			_ = json.NewDecoder(resp.Body).Decode(&out)
		}
		resp.Body.Close()
		for _, c := range out.DockerCommands {
			go runDockerCommand(panelURL, token, c)
		}
		for _, c := range out.KubeCommands {
			go runKubeCommand(panelURL, token, c)
		}
		for _, c := range out.BackupCommands {
			go runBackupCommand(panelURL, token, c)
		}
	}
}

func postDockerResult(panelURL, token string, id int, ok bool, output string) {
	body, _ := json.Marshal(map[string]any{"id": id, "ok": ok, "output": output})
	req, err := http.NewRequest("POST", strings.TrimRight(panelURL, "/")+"/api/agent/docker-result", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: docker-result не отправлен: %v\n", err)
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
}

// ============================ Kubernetes ============================
// Агент ходит в kube-api по токену ВЫДЕЛЕННОГО ServiceAccount с узким RBAC
// (kube-setup.sh): read по кластеру + точечный write (rollout restart, delete pod).
// Не cluster-admin — блок-радиус ограничен даже при компрометации панели/агента.

// kubeConf — содержимое kube.json (доступ SA к kube-api).
type kubeConf struct {
	Server string `json:"server"`
	CA     string `json:"ca"` // base64 PEM кластерного CA
	Token  string `json:"token"`
}

// имена k8s (DNS-1123): строго валидируем перед подстановкой в URL команд, чтобы
// панель не смогла заставить агента дёрнуть произвольный ресурс/путь (path-инъекция).
var kubeNameRe = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$`)

func kubeValidName(s string) bool { return kubeNameRe.MatchString(s) }

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

// procRunning — есть ли процесс с таким comm (скан /proc/*/comm).
func procRunning(name string) bool {
	ents, _ := os.ReadDir("/proc")
	for _, e := range ents {
		if _, err := strconv.Atoi(e.Name()); err != nil {
			continue
		}
		if b, err := os.ReadFile("/proc/" + e.Name() + "/comm"); err == nil {
			if strings.TrimSpace(string(b)) == name {
				return true
			}
		}
	}
	return false
}

// kubeFlavor — установлен ли кластер и какой дистрибутив ("" = нет кластера).
func kubeFlavor() string {
	switch {
	case fileExists("/usr/local/bin/k0s") || procRunning("k0s"):
		return "k0s"
	case fileExists("/etc/rancher/k3s/k3s.yaml") || procRunning("k3s") || procRunning("k3s-server"):
		return "k3s"
	case fileExists("/snap/bin/microk8s") || procRunning("microk8s"):
		return "microk8s"
	case procRunning("kube-apiserver") || fileExists("/etc/kubernetes/manifests"):
		return "kubeadm"
	case procRunning("kubelet"):
		return "kubernetes"
	}
	return ""
}

func kubeReadConf() (*kubeConf, error) {
	b, err := os.ReadFile(kubeConfigPath)
	if err != nil {
		return nil, err
	}
	var kc kubeConf
	if err := json.Unmarshal(b, &kc); err != nil {
		return nil, err
	}
	if kc.Server == "" || kc.Token == "" {
		return nil, fmt.Errorf("kube.json неполный")
	}
	return &kc, nil
}

// kubeClient — http-клиент к kube-api с пиннингом кластерного CA (без token в TLS).
// Транспорт для kube-API — ОДИН на процесс, как у docker. collectKube зовётся каждый
// цикл (каждые 15с), а kubeClient раньше создавал новый http.Transport с TLS на КАЖДЫЙ
// вызов — та же утечка keep-alive соединений, что была с docker, только к kube-apiserver
// по TCP+TLS. Кэшируем транспорт; пересобираем лишь при смене CA (на ноде он стабилен
// весь аптайм). Старый при смене закрываем, чтобы его соединения не повисли.
var (
	kubeTrMu sync.Mutex
	kubeTr   *http.Transport
	kubeTrCA string
)

func kubeTransport(caB64 string) *http.Transport {
	kubeTrMu.Lock()
	defer kubeTrMu.Unlock()
	if kubeTr != nil && kubeTrCA == caB64 {
		return kubeTr
	}
	pool := x509.NewCertPool()
	if pem, err := base64.StdEncoding.DecodeString(caB64); err == nil {
		pool.AppendCertsFromPEM(pem)
	}
	if kubeTr != nil {
		kubeTr.CloseIdleConnections() // CA сменился — не держим соединения старого
	}
	kubeTr = &http.Transport{
		TLSClientConfig:     &tls.Config{RootCAs: pool},
		MaxIdleConns:        4,
		MaxIdleConnsPerHost: 4,
		IdleConnTimeout:     60 * time.Second,
	}
	kubeTrCA = caB64
	return kubeTr
}

func kubeClient(kc *kubeConf) *http.Client {
	return &http.Client{Timeout: 8 * time.Second, Transport: kubeTransport(kc.CA)}
}

func kubeGet(cl *http.Client, kc *kubeConf, path string, out any) error {
	req, err := http.NewRequest("GET", strings.TrimRight(kc.Server, "/")+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+kc.Token)
	resp, err := cl.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("kube api %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 40_000_000)).Decode(out)
}

// collectKube — read-only снимок кластера. nil = кластера нет (секции не будет).
// Present без Access = кластер есть, но нет kube.json → панель подскажет kube-setup.
func collectKube() *kubeInfo {
	flavor := kubeFlavor()
	if flavor == "" {
		return nil
	}
	ki := &kubeInfo{Present: true, Flavor: flavor}
	kc, err := kubeReadConf()
	if err != nil {
		return ki
	}
	cl := kubeClient(kc)
	var ver struct {
		GitVersion string `json:"gitVersion"`
	}
	if err := kubeGet(cl, kc, "/version", &ver); err != nil {
		return ki
	}
	ki.Access = true
	ki.Version = ver.GitVersion

	var nodes struct {
		Items []struct {
			Metadata struct {
				Name   string
				Labels map[string]string
			}
			Status struct {
				Conditions []struct{ Type, Status string }
				NodeInfo   struct {
					KubeletVersion string
				}
				Addresses []struct{ Type, Address string }
			}
		}
	}
	if kubeGet(cl, kc, "/api/v1/nodes", &nodes) == nil {
		for _, n := range nodes.Items {
			kn := kubeNode{Name: n.Metadata.Name, Version: n.Status.NodeInfo.KubeletVersion}
			for _, c := range n.Status.Conditions {
				if c.Type == "Ready" {
					kn.Ready = c.Status == "True"
				}
			}
			var roles []string
			for k := range n.Metadata.Labels {
				if strings.HasPrefix(k, "node-role.kubernetes.io/") {
					if r := strings.TrimPrefix(k, "node-role.kubernetes.io/"); r != "" {
						roles = append(roles, r)
					}
				}
			}
			sort.Strings(roles)
			kn.Roles = strings.Join(roles, ",")
			for _, a := range n.Status.Addresses {
				if a.Type == "InternalIP" {
					kn.IP = a.Address
				}
			}
			ki.Nodes = append(ki.Nodes, kn)
		}
	}

	var ns struct{ Items []json.RawMessage }
	if kubeGet(cl, kc, "/api/v1/namespaces", &ns) == nil {
		ki.Namespaces = len(ns.Items)
	}

	ki.Workloads = append(ki.Workloads, kubeReplicaWorkloads(cl, kc, "deployments", "Deployment")...)
	ki.Workloads = append(ki.Workloads, kubeReplicaWorkloads(cl, kc, "statefulsets", "StatefulSet")...)
	ki.Workloads = append(ki.Workloads, kubeDaemonsets(cl, kc)...)
	ki.Pods = kubePods(cl, kc)
	ki.CronJobs = kubeCronJobs(cl, kc)
	ki.Volumes = kubeVolumes(cl, kc)
	// домены маршрутов кластера: стандартный Ingress + Gateway API HTTPRoute
	ki.ingressHosts = mergeSites(kubeIngressHosts(cl, kc), kubeGatewayHosts(cl, kc))
	return ki
}

// kubeGatewayHosts — домены из Gateway API (HTTPRoute.spec.hostnames): Envoy Gateway/Traefik/
// прочие, где маршруты не в Ingress. Пробуем v1, затем v1beta1. Нет CRD/прав → 404 → пусто.
// Только хосты маршрутов, без секретов. Требует gateway.networking.k8s.io/httproutes (0.14).
func kubeGatewayHosts(cl *http.Client, kc *kubeConf) []string {
	parse := func(path string) []string {
		var d struct {
			Items []struct {
				Spec struct {
					Hostnames []string `json:"hostnames"`
				} `json:"spec"`
			} `json:"items"`
		}
		if kubeGet(cl, kc, path, &d) != nil {
			return nil
		}
		var out []string
		for _, it := range d.Items {
			for _, h := range it.Spec.Hostnames {
				if h != "" {
					out = append(out, h)
				}
			}
		}
		return out
	}
	if h := parse("/apis/gateway.networking.k8s.io/v1/httproutes"); len(h) > 0 {
		return h
	}
	return parse("/apis/gateway.networking.k8s.io/v1beta1/httproutes")
}

// kubeIngressHosts — домены из Ingress-ресурсов кластера (spec.rules[].host). Панель
// вешает их на ingress-контроллер (web_services), чтобы видеть, какие сайты он обслуживает.
// Требует networking.k8s.io/ingresses:list (kube-setup v0.13); на старом RBAC вернёт 403 —
// просто отдаём пусто, не ломаемся. Только ХОСТЫ маршрутов, без секретов/TLS-содержимого.
func kubeIngressHosts(cl *http.Client, kc *kubeConf) []string {
	var d struct {
		Items []struct {
			Spec struct {
				Rules []struct {
					Host string `json:"host"`
				} `json:"rules"`
			} `json:"spec"`
		} `json:"items"`
	}
	if kubeGet(cl, kc, "/apis/networking.k8s.io/v1/ingresses", &d) != nil {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, it := range d.Items {
		for _, r := range it.Spec.Rules {
			if r.Host != "" && !seen[r.Host] {
				seen[r.Host] = true
				out = append(out, r.Host)
			}
		}
	}
	sort.Strings(out)
	return out
}

// kubeCronJobs — расписания заданий кластера (обычно единицы). Панель по ним понимает,
// что дамп СУБД уже настроен. Требует batch/cronjobs:get,list (kube-setup v2); на старом
// RBAC запрос вернёт 403 и мы просто отдадим пусто — не ломаемся.
func kubeCronJobs(cl *http.Client, kc *kubeConf) []kubeCronJob {
	var d struct {
		Items []struct {
			Metadata struct{ Name, Namespace string }
			Spec     struct {
				Schedule    string
				Suspend     *bool
				JobTemplate struct {
					Spec struct {
						Template struct {
							Spec struct {
								Containers []struct{ Image string }
							}
						}
					}
				}
			}
			Status struct {
				Active             []json.RawMessage `json:"active"`
				LastScheduleTime   string            `json:"lastScheduleTime"`
				LastSuccessfulTime string            `json:"lastSuccessfulTime"`
			}
		}
	}
	if kubeGet(cl, kc, "/apis/batch/v1/cronjobs", &d) != nil {
		return nil
	}
	unix := func(s string) int64 {
		if s == "" {
			return 0
		}
		t, err := time.Parse(time.RFC3339, s)
		if err != nil {
			return 0
		}
		return t.Unix()
	}
	out := make([]kubeCronJob, 0, len(d.Items))
	for _, c := range d.Items {
		cj := kubeCronJob{NS: c.Metadata.Namespace, Name: c.Metadata.Name, Schedule: c.Spec.Schedule}
		if c.Spec.Suspend != nil {
			cj.Suspend = *c.Spec.Suspend
		}
		if cs := c.Spec.JobTemplate.Spec.Template.Spec.Containers; len(cs) > 0 {
			cj.Image = cs[0].Image
		}
		cj.LastSchedule = unix(c.Status.LastScheduleTime)
		cj.LastSuccess = unix(c.Status.LastSuccessfulTime)
		cj.Active = len(c.Status.Active)
		out = append(out, cj)
	}
	return out
}

// kubeVolumes — постоянные тома (PV). Нужны аудиту бэкапа: том на hostPath/local — это
// обычный каталог на ноде, и его надо либо включить в бэкап, либо осознанно исключить;
// том на nfs/csi живёт вне ноды, и файловый бэкап его не заберёт в принципе.
// Требует "" persistentvolumes:get,list (kube-setup v3); на старом RBAC вернётся 403 —
// отдаём пусто и не ломаемся, как и с cronjobs.
func kubeVolumes(cl *http.Client, kc *kubeConf) []kubeVolume {
	var d struct {
		Items []struct {
			Metadata struct{ Name string }
			Spec     struct {
				Capacity         map[string]string
				StorageClassName string
				HostPath         *struct{ Path string }
				Local            *struct{ Path string }
				NFS              *struct{ Server, Path string } `json:"nfs"`
				CSI              *struct{ Driver string }
				ClaimRef         *struct{ Namespace, Name string }
				NodeAffinity     *struct {
					Required *struct {
						NodeSelectorTerms []struct {
							MatchExpressions []struct {
								Key    string
								Values []string
							}
						}
					}
				}
			}
		}
	}
	if kubeGet(cl, kc, "/api/v1/persistentvolumes", &d) != nil {
		return nil
	}
	out := make([]kubeVolume, 0, len(d.Items))
	for _, v := range d.Items {
		kv := kubeVolume{
			Name:     v.Metadata.Name,
			Class:    v.Spec.StorageClassName,
			Capacity: v.Spec.Capacity["storage"],
		}
		if c := v.Spec.ClaimRef; c != nil && c.Name != "" {
			kv.Claim = c.Namespace + "/" + c.Name
		}
		switch {
		case v.Spec.HostPath != nil:
			kv.Kind, kv.Path = "hostPath", v.Spec.HostPath.Path
		case v.Spec.Local != nil:
			kv.Kind, kv.Path = "local", v.Spec.Local.Path
		case v.Spec.NFS != nil:
			kv.Kind = "nfs"
		case v.Spec.CSI != nil:
			kv.Kind = "csi:" + v.Spec.CSI.Driver
		default:
			kv.Kind = "other"
		}
		// к какой ноде привязан том: иначе панель ныла бы на КАЖДОЙ ноде про чужие тома
		if na := v.Spec.NodeAffinity; na != nil && na.Required != nil {
			for _, term := range na.Required.NodeSelectorTerms {
				for _, e := range term.MatchExpressions {
					if e.Key == "kubernetes.io/hostname" && len(e.Values) > 0 {
						kv.Node = e.Values[0]
					}
				}
			}
		}
		out = append(out, kv)
	}
	return out
}

// kubeReplicaWorkloads — Deployment/StatefulSet: desired=spec.replicas, ready=status.readyReplicas.
func kubeReplicaWorkloads(cl *http.Client, kc *kubeConf, plural, kind string) []kubeWorkload {
	var d struct {
		Items []struct {
			Metadata struct{ Name, Namespace string }
			Spec     struct{ Replicas *int }
			Status   struct{ ReadyReplicas int }
		}
	}
	if kubeGet(cl, kc, "/apis/apps/v1/"+plural, &d) != nil {
		return nil
	}
	out := make([]kubeWorkload, 0, len(d.Items))
	for _, x := range d.Items {
		desired := 0
		if x.Spec.Replicas != nil {
			desired = *x.Spec.Replicas
		}
		out = append(out, kubeWorkload{
			NS: x.Metadata.Namespace, Kind: kind, Name: x.Metadata.Name,
			Ready: x.Status.ReadyReplicas, Desired: desired,
		})
	}
	return out
}

func kubeDaemonsets(cl *http.Client, kc *kubeConf) []kubeWorkload {
	var d struct {
		Items []struct {
			Metadata struct{ Name, Namespace string }
			Status   struct{ NumberReady, DesiredNumberScheduled int }
		}
	}
	if kubeGet(cl, kc, "/apis/apps/v1/daemonsets", &d) != nil {
		return nil
	}
	out := make([]kubeWorkload, 0, len(d.Items))
	for _, x := range d.Items {
		out = append(out, kubeWorkload{
			NS: x.Metadata.Namespace, Kind: "DaemonSet", Name: x.Metadata.Name,
			Ready: x.Status.NumberReady, Desired: x.Status.DesiredNumberScheduled,
		})
	}
	return out
}

const kubePodCap = 400 // при огромных кластерах шлём все проблемные + добор здоровыми

// credVarLike — переменная относится к доступу в БД (user/password/root/database)?
func credVarLike(n string) bool {
	u := strings.ToUpper(n)
	for _, k := range []string{"USER", "PASSWORD", "PASS", "ROOT", "DATABASE"} {
		if strings.Contains(u, k) {
			return true
		}
	}
	return false
}

// secretVarLike — переменная СЕКРЕТНАЯ (пароль/токен)? plain-значение таких НЕ отдаём.
func secretVarLike(n string) bool {
	u := strings.ToUpper(n)
	for _, k := range []string{"PASS", "SECRET", "TOKEN", "KEY"} {
		if strings.Contains(u, k) {
			return true
		}
	}
	return false
}

func kubePods(cl *http.Client, kc *kubeConf) []kubePod {
	var d struct {
		Items []struct {
			Metadata struct {
				Name, Namespace string
				OwnerReferences []struct{ Kind string }
			}
			Spec struct {
				NodeName   string
				Containers []struct {
					Image string
					Env   []struct {
						Name      string `json:"name"`
						Value     string `json:"value"`
						ValueFrom *struct {
							SecretKeyRef *struct {
								Name string `json:"name"`
								Key  string `json:"key"`
							} `json:"secretKeyRef"`
						} `json:"valueFrom"`
					} `json:"env"`
					EnvFrom []struct {
						SecretRef *struct {
							Name string `json:"name"`
						} `json:"secretRef"`
					} `json:"envFrom"`
				}
			}
			Status struct {
				Phase             string
				Reason            string
				PodIP             string
				ContainerStatuses []struct {
					Ready        bool
					RestartCount int
					State        map[string]struct{ Reason string }
				}
			}
		}
	}
	if kubeGet(cl, kc, "/api/v1/pods", &d) != nil {
		return nil
	}
	out := make([]kubePod, 0, len(d.Items))
	for _, p := range d.Items {
		kp := kubePod{
			NS: p.Metadata.Namespace, Name: p.Metadata.Name,
			Phase: p.Status.Phase, Node: p.Spec.NodeName, Reason: p.Status.Reason,
		}
		if len(p.Metadata.OwnerReferences) > 0 {
			kp.Owner = p.Metadata.OwnerReferences[0].Kind
		}
		// образ шлём ТОЛЬКО у подов, похожих на СУБД: в больших кластерах (сотни подов)
		// гонять образ каждого пода в каждом отчёте — лишние килобайты на ровном месте.
		// Точное сопоставление образ→движок и советы остаются на стороне панели.
		for _, c := range p.Spec.Containers {
			if dbImageLike(c.Image) {
				kp.Image = c.Image
				kp.ip = p.Status.PodIP
				// креды: envFrom-секреты + кред-переменные (имя+источник). Пароли plain-
				// значением НЕ тащим (утечка) — только secretKeyRef; у user/database plain ок.
				var cr kubeCred
				for _, ef := range c.EnvFrom {
					if ef.SecretRef != nil && ef.SecretRef.Name != "" {
						cr.EnvFrom = append(cr.EnvFrom, ef.SecretRef.Name)
					}
				}
				for _, e := range c.Env {
					if !credVarLike(e.Name) {
						continue
					}
					ref := kubeEnvRef{Name: e.Name}
					if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Name != "" {
						ref.Secret = e.ValueFrom.SecretKeyRef.Name
						ref.Key = e.ValueFrom.SecretKeyRef.Key
					} else if e.Value != "" && !secretVarLike(e.Name) {
						ref.Value = e.Value // plain — только НЕсекретные (user/database)
					} else {
						continue // пароль plain-значением не отдаём; без источника бесполезен
					}
					cr.Env = append(cr.Env, ref)
				}
				if len(cr.EnvFrom) > 0 || len(cr.Env) > 0 {
					kp.Cred = &cr
				}
				break
			}
		}
		ready := len(p.Status.ContainerStatuses) > 0
		for _, cs := range p.Status.ContainerStatuses {
			kp.Restarts += cs.RestartCount
			if !cs.Ready {
				ready = false
			}
			for _, st := range cs.State { // waiting/terminated несут reason (CrashLoopBackOff…)
				if st.Reason != "" && kp.Reason == "" {
					kp.Reason = st.Reason
				}
			}
		}
		kp.Ready = ready && p.Status.Phase == "Running"
		out = append(out, kp)
	}
	if len(out) > kubePodCap {
		out = kubeCapPods(out, kubePodCap)
	}
	return out
}

// kubeCapPods — приоритет проблемным подам, добор здоровыми до cap (не теряем аварии).
func kubeCapPods(pods []kubePod, cap int) []kubePod {
	res := make([]kubePod, 0, cap)
	var good []kubePod
	for _, p := range pods {
		if !p.Ready || p.Restarts > 0 || (p.Phase != "Running" && p.Phase != "Succeeded") {
			res = append(res, p)
		} else {
			good = append(good, p)
		}
	}
	for _, p := range good {
		if len(res) >= cap {
			break
		}
		res = append(res, p)
	}
	if len(res) > cap {
		res = res[:cap]
	}
	return res
}

var kubePlural = map[string]string{
	"deployment": "deployments", "statefulset": "statefulsets", "daemonset": "daemonsets",
}

// runKubeCommand исполняет управляющее действие из очереди панели через kube-api.
// Действия — ТОЛЬКО из белого списка, имена валидируются. Запускается в горутине.
func runKubeCommand(panelURL, token string, cmd kubeCommand) {
	kc, err := kubeReadConf()
	if err != nil {
		postKubeResult(panelURL, token, cmd.ID, false, "нет доступа к kube-api (kube.json)")
		return
	}
	if !kubeValidName(cmd.NS) || !kubeValidName(cmd.Name) {
		postKubeResult(panelURL, token, cmd.ID, false, "недопустимое имя/namespace")
		return
	}
	cl := kubeClient(kc)
	ok, output := false, ""
	switch cmd.Action {
	case "rollout_restart":
		plural := kubePlural[strings.ToLower(cmd.Kind)]
		if plural == "" {
			postKubeResult(panelURL, token, cmd.ID, false, "неизвестный kind")
			return
		}
		patch := fmt.Sprintf(`{"spec":{"template":{"metadata":{"annotations":{"kervax.io/restartedAt":%q}}}}}`,
			time.Now().UTC().Format(time.RFC3339))
		ok, output = kubeWrite(cl, kc, "PATCH",
			fmt.Sprintf("/apis/apps/v1/namespaces/%s/%s/%s", url.PathEscape(cmd.NS), plural, url.PathEscape(cmd.Name)),
			patch, "application/strategic-merge-patch+json")
	case "delete_pod":
		ok, output = kubeWrite(cl, kc, "DELETE",
			fmt.Sprintf("/api/v1/namespaces/%s/pods/%s", url.PathEscape(cmd.NS), url.PathEscape(cmd.Name)), "", "")
	case "logs":
		ok, output = kubePodLogs(cl, kc, cmd)
	default:
		output = "неизвестное действие"
	}
	postKubeResult(panelURL, token, cmd.ID, ok, output)
}

// kubeWrite — PATCH/DELETE к kube-api. 200/201/202 = успех.
func kubeWrite(cl *http.Client, kc *kubeConf, method, path, body, ctype string) (bool, string) {
	var rd io.Reader
	if body != "" {
		rd = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, strings.TrimRight(kc.Server, "/")+path, rd)
	if err != nil {
		return false, err.Error()
	}
	req.Header.Set("Authorization", "Bearer "+kc.Token)
	if ctype != "" {
		req.Header.Set("Content-Type", ctype)
	}
	resp, err := cl.Do(req)
	if err != nil {
		return false, err.Error()
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true, "OK"
	}
	return false, fmt.Sprintf("kube %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
}

func kubePodLogs(cl *http.Client, kc *kubeConf, cmd kubeCommand) (bool, string) {
	q := "timestamps=true"
	if cmd.Since > 0 {
		q += fmt.Sprintf("&sinceSeconds=%d", cmd.Since)
	} else {
		tail := cmd.Tail
		if tail <= 0 || tail > 20000 {
			tail = 400
		}
		q += fmt.Sprintf("&tailLines=%d", tail)
	}
	path := fmt.Sprintf("/api/v1/namespaces/%s/pods/%s/log?%s",
		url.PathEscape(cmd.NS), url.PathEscape(cmd.Name), q)
	req, err := http.NewRequest("GET", strings.TrimRight(kc.Server, "/")+path, nil)
	if err != nil {
		return false, err.Error()
	}
	req.Header.Set("Authorization", "Bearer "+kc.Token)
	lc := &http.Client{Timeout: 30 * time.Second, Transport: cl.Transport} // логи могут быть большими
	resp, err := lc.Do(req)
	if err != nil {
		return false, err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
		return false, fmt.Sprintf("logs %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 20_000_000))
	if err != nil {
		return false, "ошибка чтения логов"
	}
	return true, string(raw)
}

func postKubeResult(panelURL, token string, id int, ok bool, output string) {
	body, _ := json.Marshal(map[string]any{"id": id, "ok": ok, "output": output})
	req, err := http.NewRequest("POST", strings.TrimRight(panelURL, "/")+"/api/agent/kube-result", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: kube-result не отправлен: %v\n", err)
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
}

// ============================ Backup (restic) ============================
// Статус restic-бэкапа read-only и БЕЗ секретов: метрики node_exporter (rk.prom,
// mode 0775 — читаемы) + `systemctl show/is-*` (доступно непривилегированному юзеру).
// Обфускация клиента сохраняется — панель не видит пароли/URL/сервер назначения.

// cmdOut — запуск команды с таймаутом; возвращает stdout даже при ненулевом rc
// (systemctl is-enabled печатает "disabled" с rc!=0 — это валидный ответ).
// окружение для дочерних процессов: без служебных переменных systemd
func cleanEnv() []string {
	out := make([]string, 0, len(os.Environ()))
	for _, kv := range os.Environ() {
		if strings.HasPrefix(kv, "NOTIFY_SOCKET=") || strings.HasPrefix(kv, "WATCHDOG_PID=") ||
			strings.HasPrefix(kv, "WATCHDOG_USEC=") {
			continue
		}
		out = append(out, kv)
	}
	return out
}

func cmdOut(timeout time.Duration, name string, args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	// NOTIFY_SOCKET детям НЕ отдаём: часть утилит его подхватывает и пишет в сокет
	// systemd, а тот при NotifyAccess=main их отвергает и засоряет журнал строками
	// «Got notification message from PID …» (десятки в минуту, реальные ошибки в них тонут)
	cmd.Env = append(cleanEnv(), "HOME=/tmp")
	out, _ := cmd.Output()
	return strings.TrimSpace(string(out))
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return strings.TrimSpace(s[:i])
	}
	return strings.TrimSpace(s)
}

func systemctlShow(unit, prop string) string {
	return cmdOut(3*time.Second, "systemctl", "show", unit, "-p", prop, "--value")
}

// backupFromSystemd — фолбэк, когда prom-метрики нет (ноды со старой ансибл-раскладкой,
// её runner метрик не пишет): время и длительность последнего прогона берём из systemd.
// Человекочитаемые метки (`Sun 2026-07-19 05:05:35 MSK`) НЕ парсим — Go не резолвит такие
// TZ-аббревиатуры. Считаем по МОНОТОННЫМ меткам (мкс от загрузки) + аптайм.
func backupFromSystemd(bi *backupInfo, unit string) {
	exitMono, err1 := strconv.ParseInt(systemctlShow(unit, "ExecMainExitTimestampMonotonic"), 10, 64)
	if err1 != nil || exitMono <= 0 {
		return // сервис ещё ни разу не отработал
	}
	up := uptime()
	if up <= 0 {
		return
	}
	now := time.Now().Unix()
	ts := (now - up) + exitMono/1_000_000
	// защита от бессмыслицы (перевод часов, кривой аптайм): в будущее и в доисторию не верим
	if ts <= 0 || ts > now+60 {
		return
	}
	bi.LastBackupTs = ts
	bi.TsSource = "systemd"
	startMono, err2 := strconv.ParseInt(systemctlShow(unit, "ExecMainStartTimestampMonotonic"), 10, 64)
	if err2 == nil && startMono > 0 && exitMono > startMono {
		bi.DurationSec = (exitMono - startMono) / 1_000_000
	}
}

// fullBackupTiming — полная длительность ПОСЛЕДНЕГО запуска сервиса (дампы+restic) и его
// старт. ExecMainStart отражает только restic (после ExecStartPre-дампов), поэтому берём
// границы всего юнита: InactiveExit (юнит вышел из простоя = старт) → InactiveEnter (вернулся
// в простой = полностью завершился). Разница = дампы + restic. Моно-метки + аптайм, как в
// backupFromSystemd (человекочитаемые TZ-метки Go не парсит).
func fullBackupTiming(bi *backupInfo, unit string) {
	exitMono, e1 := strconv.ParseInt(systemctlShow(unit, "InactiveExitTimestampMonotonic"), 10, 64)   // старт запуска
	enterMono, e2 := strconv.ParseInt(systemctlShow(unit, "InactiveEnterTimestampMonotonic"), 10, 64) // конец запуска
	if e1 != nil || e2 != nil || exitMono <= 0 {
		return
	}
	if enterMono > exitMono {
		bi.FullDurationSec = (enterMono - exitMono) / 1_000_000
	}
	up := uptime()
	if up > 0 {
		now := time.Now().Unix()
		st := (now - up) + exitMono/1_000_000
		if st > 0 && st <= now+60 {
			bi.StartedTs = st
		}
	}
}

// metricVal — значение метрики prometheus по имени (берём последнюю совпавшую строку).
func metricVal(content, name string) (float64, bool) {
	var v float64
	found := false
	for _, ln := range strings.Split(content, "\n") {
		if strings.HasPrefix(ln, name+"{") || strings.HasPrefix(ln, name+" ") {
			f := strings.Fields(ln)
			if len(f) >= 2 {
				if x, err := strconv.ParseFloat(f[len(f)-1], 64); err == nil {
					v, found = x, true
				}
			}
		}
	}
	return v, found
}

// readResticMetrics — самый свежий *.prom с restic_last_backup_* (по timestamp).
func readResticMetrics(bi *backupInfo) {
	var best int64 = -1
	for _, d := range []string{
		"/var/lib/node_exporter/textfile_collector",
		"/var/lib/prometheus/node-exporter",
	} {
		ents, _ := os.ReadDir(d)
		for _, e := range ents {
			if !strings.HasSuffix(e.Name(), ".prom") {
				continue
			}
			b, err := os.ReadFile(filepath.Join(d, e.Name()))
			if err != nil || !strings.Contains(string(b), "restic_last_backup_") {
				continue
			}
			s := string(b)
			ts, ok := metricVal(s, "restic_last_backup_timestamp")
			if !ok || int64(ts) <= best {
				continue
			}
			best = int64(ts)
			bi.MetricPresent = true
			bi.LastBackupTs = int64(ts)
			if suc, ok := metricVal(s, "restic_last_backup_success"); ok {
				iv := int(suc)
				bi.Success = &iv
			}
			if sk, ok := metricVal(s, "restic_last_backup_skipped"); ok {
				bi.Skipped = int(sk)
			}
			if dur, ok := metricVal(s, "restic_last_backup_duration_seconds"); ok {
				bi.DurationSec = int64(dur)
			}
		}
	}
}

// rotationStat — состояние чистки одного репозитория, из restic_server_*.prom.
type rotationStat struct {
	ts      int64
	ok      int
	removed int
	oldest  int64
}

// readResticServerMetrics — метрики СЕРВЕРНОЙ ротации, по клиенту.
//
// Отдельно от readResticMetrics намеренно: там статус бэкапа («снялся ли»), тут статус
// ротации («чистится ли»). Раньше эти файлы не читал никто: агент фильтровал строго
// restic_last_backup_, а node-exporter в них не смотрел — и мёртвая ротация 17 дней
// выглядела зелёной, потому что её просто не измеряли.
func readResticServerMetrics() map[string]rotationStat {
	out := map[string]rotationStat{}
	for _, d := range []string{
		"/var/lib/node_exporter/textfile_collector",
		"/var/lib/prometheus/node-exporter",
	} {
		ents, _ := os.ReadDir(d)
		for _, e := range ents {
			if !strings.HasSuffix(e.Name(), ".prom") {
				continue
			}
			b, err := os.ReadFile(filepath.Join(d, e.Name()))
			if err != nil || !strings.Contains(string(b), "restic_server_") {
				continue
			}
			txt := string(b)
			// имя клиента берём из МЕТКИ, а не из имени файла: имя файла санитизируется,
			// а метка — то же значение, что видит панель в списке репозиториев
			client := labelVal(txt, "client")
			if client == "" {
				continue
			}
			r := rotationStat{ok: -1, removed: -1}
			if v, okv := metricVal(txt, "restic_server_prune_timestamp"); okv {
				r.ts = int64(v)
			}
			if v, okv := metricVal(txt, "restic_server_prune_success"); okv {
				r.ok = int(v)
			}
			if v, okv := metricVal(txt, "restic_server_forget_removed"); okv {
				r.removed = int(v)
			}
			if v, okv := metricVal(txt, "restic_server_oldest_snapshot_timestamp"); okv {
				r.oldest = int64(v)
			}
			out[client] = r
		}
	}
	return out
}

// labelVal — значение метки из первой строки, где она встречается: client="agent".
func labelVal(content, label string) string {
	pfx := label + `="`
	for _, ln := range strings.Split(content, "\n") {
		i := strings.Index(ln, pfx)
		if i < 0 {
			continue
		}
		rest := ln[i+len(pfx):]
		if j := strings.IndexByte(rest, '"'); j >= 0 {
			return rest[:j]
		}
	}
	return ""
}

// dumpStat — включённый дамп СУБД: сколько файлов лежит, сколько места, когда последний.
type dumpStat struct {
	Engine string `json:"engine"`
	// контейнер, чью базу дампим: движков одного типа на ноде бывает несколько,
	// и панель должна показывать состояние каждого отдельно (пусто = нативная установка)
	Container string `json:"container,omitempty"`
	Files     int    `json:"files"`
	SizeBytes int64  `json:"size_bytes"`
	LastTs    int64  `json:"last_ts"`
	Keep      int    `json:"keep"`
	// настройки и статус защиты от переполнения (helper 0.13+)
	MinFreePct  int    `json:"min_free_pct,omitempty"`
	Dir         string `json:"dir,omitempty"`
	Skipped     bool   `json:"skipped,omitempty"` // последний прогон пропущен: мало места
	SkipTs      int64  `json:"skip_ts,omitempty"`
	SkipFreePct int    `json:"skip_free_pct,omitempty"`
	// когда дамп включён (mtime скрипта). Панель по нему даёт grace: сразу после
	// включения «файлов нет» — норма, а не поломка (первый дамп в ближайший бэкап).
	EnabledTs int64 `json:"enabled_ts,omitempty"`
}

// collectBackup — nil, если следов restic-бэкапа нет (секции в панели не будет).
func collectBackup() *backupInfo {
	bins := []string{"/usr/local/lib/.restic/restic", "/usr/bin/restic", "/usr/local/bin/restic"}
	timers := []string{"systemd-rest.timer", "restic-backup.timer", "restic.timer"}
	services := []string{"systemd-rest.service", "restic-backup.service", "restic.service"}
	configs := []string{"/etc/systemd-resta.conf", "/etc/systemd-rest.conf", "/etc/restic.env", "/etc/restic-backup.env"}

	bi := &backupInfo{}
	for _, p := range bins {
		if fi, err := os.Stat(p); err == nil && fi.Mode()&0o111 != 0 {
			bi.ResticFound = true
			bi.ResticVersion = firstLine(cmdOut(3*time.Second, p, "version"))
			break
		}
	}
	var timerUnit string
	for _, t := range timers {
		if fileExists("/etc/systemd/system/"+t) || strings.Contains(cmdOut(3*time.Second, "systemctl", "list-unit-files", "--no-legend", t), t) {
			timerUnit = t
			break
		}
	}
	if timerUnit != "" {
		bi.Configured = true
		bi.TimerEnabled = cmdOut(3*time.Second, "systemctl", "is-enabled", timerUnit) == "enabled"
		bi.TimerActive = cmdOut(3*time.Second, "systemctl", "is-active", timerUnit) == "active"
	}
	var serviceUnit string
	for _, s := range services {
		if fileExists("/etc/systemd/system/" + s) {
			serviceUnit = s
			bi.ServiceResult = systemctlShow(s, "Result")
			break
		}
	}
	configFound := false
	for _, f := range configs {
		if fileExists(f) {
			configFound = true
			break
		}
	}
	readResticMetrics(bi)
	// метрики нет (старая ансибл-раскладка её не пишет) → берём время прогона из systemd,
	// иначе панель показывала бы «—» и не видела протухания у полностью рабочего бэкапа
	if !bi.MetricPresent && serviceUnit != "" {
		backupFromSystemd(bi, serviceUnit)
	}
	// полная длительность (дампы + restic) — всегда из systemd, даже когда время берём из
	// prom-метрики: та знает только restic-фазу, а нам нужно сколько шёл весь запуск
	if serviceUnit != "" {
		fullBackupTiming(bi, serviceUnit)
	}

	// helper может стоять и БЕЗ файлового бэкапа — тогда единственный признак это его
	// конфиг-файл, где лежат manageable и включённые ЛОКАЛЬНЫЕ дампы (свой таймер).
	// Раньше блок в таком случае не отправлялся вовсе: панель не видела ни helper'а,
	// ни снятых дампов и предлагала поставить то, что уже стоит.
	helperCfg := fileExists(backupConfigFile)
	bi.Present = bi.ResticFound || bi.Configured || configFound || bi.MetricPresent || helperCfg
	if !bi.Present {
		return nil
	}
	// Заметки — только про СЛОМАННЫЙ файловый бэкап. Если блок отдаём лишь ради helper'а
	// и локальных дампов, «restic не найден» читалось бы как поломка, хотя файлового
	// бэкапа тут и не планировалось.
	if bi.ResticFound || bi.Configured || configFound || bi.MetricPresent {
		if !bi.ResticFound {
			bi.Notes = append(bi.Notes, "restic бинарь не найден")
		}
		if timerUnit == "" {
			bi.Notes = append(bi.Notes, "таймер бэкапа не найден")
		}
		if !bi.MetricPresent {
			if bi.TsSource == "systemd" {
				bi.Notes = append(bi.Notes,
					"метрики restic нет (старый runner) — время и длительность взяты из systemd")
			} else {
				bi.Notes = append(bi.Notes, "метрика restic не найдена")
			}
		}
	}
	// обогащение конфигом (Фаза 2): его пишет root-таймер (backup-setup.sh) в файл,
	// т.к. агент под NoNewPrivileges не может sudo. Без файла — бэкап виден по статусу,
	// просто без управления.
	if b, err := os.ReadFile(backupConfigFile); err == nil {
		var cfg struct {
			Manageable    bool       `json:"manageable"`
			HelperVersion int        `json:"helper_version"`
			Mode          string     `json:"mode"`
			Schedule      string     `json:"schedule"`
			Includes      []string   `json:"includes"`
			Excludes      []string   `json:"excludes"`
			RepoDest      string     `json:"repo_dest"`
			Dumps         []dumpStat `json:"dumps"`
		}
		if json.Unmarshal(b, &cfg) == nil && cfg.Manageable {
			bi.Manageable = true
			bi.HelperVersion = cfg.HelperVersion
			bi.Mode = cfg.Mode
			bi.Schedule = cfg.Schedule
			bi.Includes = cfg.Includes
			bi.Excludes = cfg.Excludes
			bi.RepoDest = cfg.RepoDest
			bi.Dumps = cfg.Dumps
		}
	}
	return bi
}

// Привилегированные бэкап-операции идут ЧЕРЕЗ ФАЙЛЫ, а не sudo: агент под
// NoNewPrivileges не может повышать привилегии. root-таймеры (setup-скрипты) пишут
// stats/config в *-json, а команды агент кладёт в спул backup-req, где их исполняет
// root-процессор (path-unit) и пишет ответ в backup-res. Агент остаётся полностью изолирован.
const backupStatsFile = "/var/lib/kervax/backupserver.json"
const backupConfigFile = "/var/lib/kervax/backup-config.json"
const backupReqDir = "/var/lib/kervax/backup-req"
const backupResDir = "/var/lib/kervax/backup-res"
const bsrvReqDir = "/var/lib/kervax/bsrv-req" // спул бэкап-СЕРВЕРА (провижининг клиентов/tls)
const bsrvResDir = "/var/lib/kervax/bsrv-res"
const tsyncReqDir = "/var/lib/kervax/tsync-req" // спул синхронизации времени (timesync-setup)
const tsyncResDir = "/var/lib/kervax/tsync-res"

// collectBackupServer — nil, если нода не сервер бэкапов. Детект по docker-контейнеру
// rest-server (без прав); per-repo статистику (снапшоты/свежесть/лок) добавляет helper
// (backupserver-setup.sh) — без restic и без паролей.
func collectBackupServer(dk *dockerInfo) *backupServerInfo {
	bs := &backupServerInfo{}
	seenInDocker := false
	if dk != nil && dk.Access {
		for _, c := range dk.Containers {
			if strings.Contains(c.Image, "rest-server") || c.Name == "rest-server" {
				bs.Present = true
				bs.Running = c.State == "running"
				seenInDocker = true
				if i := strings.LastIndex(c.Image, ":"); i >= 0 {
					bs.Version = c.Image[i+1:]
				}
				break
			}
		}
	}
	// per-repo статистику пишет root-таймер (backupserver-setup.sh) в файл — без sudo.
	if b, err := os.ReadFile(backupStatsFile); err == nil {
		var st struct {
			Present       bool       `json:"present"`
			Version       string     `json:"version"`
			HelperVersion int        `json:"helper_version"`
			Running       bool       `json:"running"`
			TLSFront      bool       `json:"tls_front"`
			TLSPort       int        `json:"tls_port"`
			DataDir       string     `json:"data_dir"`
			DiskTotal     int64      `json:"disk_total"`
			DiskUsed      int64      `json:"disk_used"`
			DiskFree      int64      `json:"disk_free"`
			Repos         []repoStat `json:"repos"`
		}
		if json.Unmarshal(b, &st) == nil && st.Present {
			bs.Present = true
			// без docker-прокси агент не видит состояние контейнера — верим helper'у (root).
			// Иначе свежий бэкап-сервер выглядел бы остановленным (ложный алерт).
			if !seenInDocker {
				bs.Running = st.Running
			}
			if st.Version != "" && bs.Version == "" {
				bs.Version = st.Version
			}
			bs.HelperVersion = st.HelperVersion
			bs.TLSFront = st.TLSFront
			bs.TLSPort = st.TLSPort
			// ротацию читаем сами: helper её не видит (метрики пишет prune-скрипт),
			// а панели нужна именно связка «репозиторий → жива ли его чистка»
			rot := readResticServerMetrics()
			for i := range st.Repos {
				r, okr := rot[st.Repos[i].Name]
				if !okr {
					st.Repos[i].RotationOK, st.Repos[i].RotationRemoved = -1, -1
					continue
				}
				st.Repos[i].RotationTs = r.ts
				st.Repos[i].RotationOK = r.ok
				st.Repos[i].RotationRemoved = r.removed
				st.Repos[i].OldestSnapshot = r.oldest
			}
			bs.DataDir = st.DataDir
			bs.DiskTotal = st.DiskTotal
			bs.DiskUsed = st.DiskUsed
			bs.DiskFree = st.DiskFree
			bs.Repos = st.Repos
		}
	}
	if !bs.Present {
		return nil
	}
	return bs
}

var backupTimeRe = regexp.MustCompile(`^([01][0-9]|2[0-3]):[0-5][0-9]$`)

// backupPathOK — путь абсолютный, только безопасные символы, без «..» (защита от
// инъекции в helper; helper валидирует повторно).
func backupPathOK(p string) bool {
	if !strings.HasPrefix(p, "/") || strings.Contains(p, "..") {
		return false
	}
	for _, r := range p {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' ||
			r == '/' || r == '.' || r == '_' || r == '-' || r == '+') { // '+' — /lost+found
			return false
		}
	}
	return true
}

// runBackupCommand кладёт валидированный запрос в спул backup-req (root-процессор его
// исполнит через узкий helper и запишет ответ в backup-res). Формат req/res — простые
// строки key=value (легко парсить в bash-процессоре). Агент не повышает привилегии.
func runBackupCommand(panelURL, token string, cmd backupCommand) {
	// валидация ДО записи в спул (процессор/helper валидируют повторно)
	var lines []string
	switch cmd.Action {
	case "set_paths":
		if cmd.Mode != "include" && cmd.Mode != "exclude" {
			postBackupResult(panelURL, token, cmd.ID, false, "неизвестный режим")
			return
		}
		if len(cmd.Paths) == 0 {
			postBackupResult(panelURL, token, cmd.ID, false, "пустой список путей")
			return
		}
		lines = []string{"action=set_paths", "mode=" + cmd.Mode}
		for _, p := range cmd.Paths {
			if !backupPathOK(p) {
				postBackupResult(panelURL, token, cmd.ID, false, "недопустимый путь: "+p)
				return
			}
			lines = append(lines, "path="+p)
		}
	case "set_schedule":
		if !backupTimeRe.MatchString(cmd.Schedule) {
			postBackupResult(panelURL, token, cmd.ID, false, "неверное время (нужно HH:MM)")
			return
		}
		lines = []string{"action=set_schedule", "schedule=" + cmd.Schedule}
	case "run_now":
		lines = []string{"action=run_now"}
	case "restic_update":
		lines = []string{"action=restic_update"}
	case "get_creds":
		lines = []string{"action=get_creds"}
	case "dump_setup", "dump_remove":
		// движок из белого списка, имя контейнера — только безопасные символы
		// список ДОЛЖЕН совпадать с _DUMP_ENGINE панели и разбором в backup-setup.sh:
		// grafana и neo4j тут отсутствовали, и кнопка «включить дампы» для них
		// отбивалась агентом, хотя и панель, и helper их умеют
		switch cmd.Engine {
		case "pg", "mysql", "ch", "redis", "rabbitmq", "k8s", "grafana", "neo4j":
		default:
			postBackupResult(panelURL, token, cmd.ID, false, "неизвестный движок дампа")
			return
		}
		if cmd.Container != "" && !backupNameOK(cmd.Container) {
			postBackupResult(panelURL, token, cmd.ID, false, "недопустимое имя контейнера")
			return
		}
		lines = []string{"action=" + cmd.Action, "engine=" + cmd.Engine, "container=" + cmd.Container}
		// параметры дампа. dir проверяем здесь ЖЁСТКО (уходит в rm/mkdir от root в helper):
		// абсолютный путь, без «..», только безопасные символы. helper валидирует повторно.
		if cmd.Action == "dump_setup" {
			if d := cmd.DumpDir; d != "" {
				if !strings.HasPrefix(d, "/") || strings.Contains(d, "..") || !dumpDirOK(d) {
					postBackupResult(panelURL, token, cmd.ID, false, "недопустимый каталог дампов")
					return
				}
				lines = append(lines, "dump_dir="+d)
			}
			if cmd.DumpKeep > 0 {
				lines = append(lines, "dump_keep="+strconv.Itoa(cmd.DumpKeep))
			}
			// 0 — валидное значение (защита выкл), поэтому шлём всегда, а не при >0
			lines = append(lines, "dump_minfree="+strconv.Itoa(cmd.DumpMinFree))
		}
	case "provision":
		// создать бэкап клиенту с нуля (restic+env+скрипт+timer). Секреты в спуле 0600.
		if !strings.HasPrefix(cmd.RepoURL, "rest:") || cmd.Repopass == "" {
			postBackupResult(panelURL, token, cmd.ID, false, "нет repo_url/repopass")
			return
		}
		if cmd.Mode != "include" && cmd.Mode != "exclude" {
			postBackupResult(panelURL, token, cmd.ID, false, "неизвестный режим")
			return
		}
		if !backupTimeRe.MatchString(cmd.Schedule) || len(cmd.Paths) == 0 {
			postBackupResult(panelURL, token, cmd.ID, false, "нет времени/путей")
			return
		}
		delay := cmd.Delay
		if delay == "" {
			delay = "1h"
		}
		ver := cmd.ResticVersion
		if ver == "" {
			ver = "0.18.1"
		}
		cacert := cmd.CacertB64
		if cacert == "" {
			cacert = "-"
		}
		lines = []string{
			"action=provision", "repo_url=" + cmd.RepoURL, "repopass=" + cmd.Repopass,
			"mode=" + cmd.Mode, "schedule=" + cmd.Schedule, "delay=" + delay,
			"restic_version=" + ver, "cacert_b64=" + cacert,
		}
		for _, p := range cmd.Paths {
			if !backupPathOK(p) {
				postBackupResult(panelURL, token, cmd.ID, false, "недопустимый путь: "+p)
				return
			}
			lines = append(lines, "path="+p)
		}
	case "deploy_server", "update_image", "provision_client", "deploy_tls_front", "get_cert", "get_client_creds":
		// команды бэкап-СЕРВЕРА → отдельный спул bsrv-req
		lines, ok := backupServerLines(cmd)
		if !ok {
			postBackupResult(panelURL, token, cmd.ID, false, "неверные параметры провижининга")
			return
		}
		res, out := spoolServerRequest(cmd.ID, lines)
		postBackupResult(panelURL, token, cmd.ID, res, out)
		return
	case "timesync":
		// синхронизация времени → отдельный спул tsync-req. panel_url отдаём helper'у для
		// HTTP-фолбэка (шаг часов по времени панели, если исходящий NTP закрыт).
		res, out := spoolTimesyncRequest(cmd.ID, []string{"action=sync", "panel_url=" + panelURL})
		postBackupResult(panelURL, token, cmd.ID, res, out)
		return
	default:
		postBackupResult(panelURL, token, cmd.ID, false, "неизвестное действие")
		return
	}
	ok, output := spoolBackupRequest(cmd.ID, lines)
	postBackupResult(panelURL, token, cmd.ID, ok, output)
}

// backupServerLines валидирует и строит строки запроса для бэкап-сервера.
func backupServerLines(cmd backupCommand) ([]string, bool) {
	switch cmd.Action {
	case "deploy_server":
		// поднять rest-server с нуля; порт валидируем тут же (helper проверит ещё раз)
		if cmd.Port < 1024 || cmd.Port > 65535 {
			return nil, false
		}
		return []string{"action=deploy_server", fmt.Sprintf("port=%d", cmd.Port)}, true
	case "update_image":
		// обновить образ rest-server до зашитого в helper — параметров нет
		return []string{"action=update_image"}, true
	case "provision_client":
		if !backupNameOK(cmd.Name) || cmd.Hpass == "" || cmd.Repopass == "" || !backupIPOK(cmd.ClientIP) {
			return nil, false
		}
		return []string{
			"action=provision_client", "name=" + cmd.Name, "hpass=" + cmd.Hpass,
			"repopass=" + cmd.Repopass, "client_ip=" + cmd.ClientIP,
			fmt.Sprintf("keep_last=%d", cmd.KeepLast), fmt.Sprintf("keep_daily=%d", cmd.KeepDaily),
			fmt.Sprintf("keep_weekly=%d", cmd.KeepWeekly), fmt.Sprintf("keep_monthly=%d", cmd.KeepMonthly),
		}, true
	case "deploy_tls_front":
		if !backupIPOK(cmd.SanIP) {
			return nil, false
		}
		return []string{"action=deploy_tls_front", "san_ip=" + cmd.SanIP, "san_dns=" + cmd.SanDNS}, true
	case "get_cert":
		return []string{"action=get_cert"}, true
	case "get_client_creds":
		if !backupNameOK(cmd.Name) {
			return nil, false
		}
		return []string{"action=get_client_creds", "name=" + cmd.Name}, true
	}
	return nil, false
}

// backupNameOK — имя клиента/репо: hostname-безопасные символы.
func backupNameOK(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' ||
			r == '.' || r == '_' || r == '-') {
			return false
		}
	}
	return true
}

// dumpDirOK — абсолютный путь под каталог дампов: только безопасные символы. Путь уходит
// в rm/mkdir от root в helper, поэтому шлём лишь то, из чего вредного пути не собрать.
// Проверку на «/» и «..» делает вызывающий; helper валидирует всё повторно.
func dumpDirOK(s string) bool {
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' ||
			r == '.' || r == '_' || r == '-' || r == '/') {
			return false
		}
	}
	return true
}

// backupIPOK — IPv4/IPv6-подобная строка (helper валидирует повторно).
func backupIPOK(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f' || r >= 'A' && r <= 'F' || r == '.' || r == ':') {
			return false
		}
	}
	return true
}

// spoolBackupRequest — запрос в спул клиента (backup-req/res).
func spoolBackupRequest(cmdID int, lines []string) (bool, string) {
	return spoolIn(backupReqDir, backupResDir, cmdID, lines, "backup-setup")
}

// spoolServerRequest — запрос в спул бэкап-сервера (bsrv-req/res).
func spoolServerRequest(cmdID int, lines []string) (bool, string) {
	return spoolIn(bsrvReqDir, bsrvResDir, cmdID, lines, "backupserver-setup")
}

// spoolTimesyncRequest — запрос в спул синхронизации времени (tsync-req/res).
func spoolTimesyncRequest(cmdID int, lines []string) (bool, string) {
	return spoolIn(tsyncReqDir, tsyncResDir, cmdID, lines, "timesync-setup")
}

// spoolIn — атомарно кладёт запрос в <reqDir> и ждёт ответ в <resDir>. Файл 0600:
// запросы могут нести секреты (repopass/hpass), процессор их сразу удаляет.
func spoolIn(reqDir, resDir string, cmdID int, lines []string, helper string) (bool, string) {
	reqID := fmt.Sprintf("%d-%d", cmdID, time.Now().UnixNano())
	tmp := filepath.Join(reqDir, reqID+".tmp")
	req := filepath.Join(reqDir, reqID+".req")
	res := filepath.Join(resDir, reqID+".res")
	body := strings.Join(lines, "\n") + "\n"
	if err := os.WriteFile(tmp, []byte(body), 0o600); err != nil {
		return false, "спул недоступен (" + helper + " не установлен?)"
	}
	if err := os.Rename(tmp, req); err != nil { // атомарно → процессор не увидит недописанный
		os.Remove(tmp)
		return false, "не удалось поставить запрос в спул"
	}
	deadline := time.Now().Add(90 * time.Second)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(res); err == nil {
			os.Remove(res) // res-каталог 0770 → агент может удалить прочитанный ответ
			ok := false
			out := ""
			for _, ln := range strings.Split(string(b), "\n") {
				if v, found := strings.CutPrefix(ln, "ok="); found {
					ok = strings.TrimSpace(v) == "true"
				} else if v, found := strings.CutPrefix(ln, "output="); found {
					out = strings.TrimSpace(v)
				}
			}
			return ok, out
		}
		time.Sleep(400 * time.Millisecond)
	}
	os.Remove(req)
	return false, "процессор не ответил (таймаут; установлен ли " + helper + "?)"
}

func postBackupResult(panelURL, token string, id int, ok bool, output string) {
	body, _ := json.Marshal(map[string]any{"id": id, "ok": ok, "output": output})
	req, err := http.NewRequest("POST", strings.TrimRight(panelURL, "/")+"/api/agent/backup-result", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: backup-result не отправлен: %v\n", err)
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
}

func readConfig(path string) (url, token string) {
	f, err := os.Open(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "kervax-agent: не открыть конфиг %s: %v\n", path, err)
		os.Exit(1)
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch strings.TrimSpace(k) {
		case "url":
			url = strings.TrimSpace(v)
		case "token":
			token = strings.TrimSpace(v)
		case "docker_host": // где читать Docker: unix-путь или tcp://host:port
			if h := strings.TrimSpace(v); h != "" {
				dockerHost = h
			}
		case "kube_config": // путь к kube.json выделенного SA
			if h := strings.TrimSpace(v); h != "" {
				kubeConfigPath = h
			}
		}
	}
	return
}

func send(url, token string, r report) (config, error) {
	// wall-clock ставим прямо перед отправкой (не в collect): так панельный сдвиг = только
	// сетевая задержка, а не «collect занял 2с». Порог алерта её с запасом перекрывает.
	r.ClockUnix = time.Now().Unix()
	raw, _ := json.Marshal(r)
	// Тело сжимаем: DPI рубит соединение по счётчику отданных байт (~16-24 КБ,
	// см. panelTransport). Отчёт в 13 КБ ужимается примерно до 2 КБ, самый жирный
	// в парке (96 КБ) — до ~15 КБ, то есть запас остаётся даже там. Панель понимает
	// и несжатое тело, так что при сбое упаковки просто шлём как раньше.
	body, enc := raw, ""
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, werr := zw.Write(raw); werr == nil && zw.Close() == nil {
		body, enc = buf.Bytes(), "gzip"
	}
	endpoint := strings.TrimRight(url, "/") + "/api/agent/report"
	mkReq := func() (*http.Request, error) {
		req, err := http.NewRequest("POST", endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if enc != "" {
			req.Header.Set("Content-Encoding", enc)
		}
		req.Header.Set("Authorization", "Bearer "+token)
		return req, nil
	}
	req, err := mkReq()
	if err != nil {
		return config{}, err
	}
	resp, err := reportClient.Do(req)
	if err != nil {
		// Один ретрай на свежем соединении: разовые обрывы случаются и без DPI.
		if req2, err2 := mkReq(); err2 == nil {
			resp, err = reportClient.Do(req2)
		}
	}
	if err != nil {
		return config{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return config{}, fmt.Errorf("панель ответила %d", resp.StatusCode)
	}
	var c config
	json.NewDecoder(resp.Body).Decode(&c)
	return c, nil
}

// sdNotify — уведомление systemd через $NOTIFY_SOCKET (raw, без внешних зависимостей).
// No-op, если сокета нет (юнит не Type=notify) → бинарь безопасно работает и под старым
// юнитом (Type=simple): READY/WATCHDOG просто игнорируются.
// Транспорт для связи с панелью. Два обязательных свойства, оба выстраданы:
//
//  1. HTTP/1.1, без HTTP/2. У h2 соединение мультиплексировано и живёт долго; если
//     путь до панели обрывается молча (правило фаервола, NAT, рестарт прокси), клиент
//     этого не замечает и продолжает писать в мёртвое соединение — каждый запрос ждёт
//     полный таймаут и падает, и так до перезапуска процесса. На HTTP/1.1 Go ловит
//     разрыв сразу и сам повторяет запрос на свежем соединении.
//  2. Отчёты идём БЕЗ keep-alive — соединение на отчёт. У провайдеров с DPI (РФ)
//     соединение убивается по счётчику ОТДАННЫХ вверх байт: замерено на ноде за DPI,
//     до 18 КБ ответ за 0.1 с, с 32 КБ — глухо; три отчёта подряд в одном соединении
//     умирают на втором, те же три в разных проходят все. Причём умирает тихо: запрос
//     уходит, ответ не возвращается никогда, и агент продолжает писать в чёрную дыру.
//     Свежее соединение обнуляет счётчик, а тело вдобавок сжимаем (см. send).
//     Прежний комментарий винил ECMP — это была неверная догадка, замеры её не подтвердили.
var (
	reportTransport = panelTransport(false)
	reportClient    = &http.Client{Timeout: 15 * time.Second, Transport: reportTransport}
)

func panelTransport(keepAlive bool) *http.Transport {
	return &http.Transport{
		ForceAttemptHTTP2:     false,
		DisableKeepAlives:     !keepAlive,
		TLSHandshakeTimeout:   10 * time.Second,
		ResponseHeaderTimeout: 12 * time.Second,
		IdleConnTimeout:       60 * time.Second,
	}
}

func sdNotify(state string) {
	sock := os.Getenv("NOTIFY_SOCKET")
	if sock == "" {
		return
	}
	name := sock
	if strings.HasPrefix(sock, "@") { // абстрактный сокет: '@' → NUL
		name = "\x00" + sock[1:]
	}
	conn, err := net.DialUnix("unixgram", nil, &net.UnixAddr{Name: name, Net: "unixgram"})
	if err != nil {
		return
	}
	defer conn.Close()
	conn.Write([]byte(state))
}

func main() {
	cfgPath := "/etc/kervax-agent.conf"
	if len(os.Args) > 1 {
		cfgPath = os.Args[1]
	}
	url, token := readConfig(cfgPath)
	if url == "" || token == "" {
		fmt.Fprintln(os.Stderr, "kervax-agent: в конфиге нужны url= и token=")
		os.Exit(1)
	}
	dialTarget = dialTargetFromURL(url)
	hostCPUModel = cpuModel()
	hostIsVM, hostVirt = detectVirt()
	fmt.Printf("kervax-agent %s → %s\n", version, url)

	interval := 15 * time.Second
	prev := snap()
	time.Sleep(time.Second) // чтобы первый cpu%/сеть были осмысленными

	// Вотчдог цикла: если сбор/отправка зависли дольше maxCycle (наблюдалось на
	// busy-postgres-ноде — I/O-столл под давлением памяти/OOM в collect(), либо
	// Statfs на подвисшем маунте; таймаута у этих syscall'ов нет), агент навсегда
	// замолкал БЕЗ ошибки в логе. Тут выходим → systemd (Restart=always) поднимет
	// свежий процесс. 90с < offline_after (120с) → ложного «оффлайна» не будет.
	// быстрый опрос docker-команд — отдельно от метрик, чтобы restart/logs шли ~3с
	go commandLoop(url, token)

	const maxCycle = 90 * time.Second
	kick := make(chan struct{}, 1)
	go func() {
		for {
			select {
			case <-kick:
			case <-time.After(maxCycle):
				fmt.Fprintf(os.Stderr,
					"kervax-agent: цикл завис >%s — перезапуск через systemd\n", maxCycle)
				os.Exit(1)
			}
		}
	}()

	// systemd Type=notify: сообщаем «готов». WATCHDOG=1 шлём с КАЖДОЙ итерации цикла — если
	// сбор/отправка зависли (Go-вотчдог однажды не сработал — см. инцидент с зависшим агентом),
	// systemd по WatchdogSec сам гасит и поднимает процесс. NOTIFY_SOCKET нет → no-op.
	sdNotify("READY=1")

	// Третий страж — ПО РЕЗУЛЬТАТУ, а не по «шевелится ли цикл». Тот же инцидент
	// 28.07: агент 11 часов исправно кормил оба вотчдога и держал соединение с панелью,
	// но отчёты до неё не доходили и ошибок в лог не писал; помог только ручной рестарт.
	// Прежние стражи ловят «цикл встал», а такое состояние для них выглядит здоровым.
	// Порог с большим запасом к интервалу отчётов: разовые сетевые сбои переживаем.
	//
	// Проверку держим В ОТДЕЛЬНОЙ ГОРУТИНЕ, а не в теле цикла: если цикл зависнет
	// где-то ВНУТРИ итерации, проверка в его начале просто не выполнится — ровно та
	// слепая зона, которую страж и закрывает (первая версия 1.86 на той ноде так
	// и не сработала: цикл жил, отчёты не доходили, страж ждал своей очереди).
	const maxSilence = 10 * time.Minute
	lastOK := &atomic.Int64{}
	lastOK.Store(time.Now().Unix())
	go func() {
		for range time.Tick(30 * time.Second) {
			if time.Since(time.Unix(lastOK.Load(), 0)) > maxSilence {
				fmt.Fprintf(os.Stderr,
					"kervax-agent: панель не приняла ни одного отчёта за %s — перезапуск через systemd\n",
					maxSilence)
				os.Exit(1)
			}
		}
	}()

	for {
		sdNotify("WATCHDOG=1") // прогресс цикла для systemd-watchdog (юнит: WatchdogSec)
		select {               // «кик» Go-вотчдогу (быстрый путь, 90с): цикл жив
		case kick <- struct{}{}:
		default:
		}
		r, cur := collect(prev)
		prev = cur
		cfg, err := send(url, token, r)
		if err != nil {
			fmt.Fprintf(os.Stderr, "kervax-agent: отправка не удалась: %v\n", err)
		} else {
			lastOK.Store(time.Now().Unix()) // отчёт реально принят панелью
			if cfg.Interval > 0 {
				interval = time.Duration(cfg.Interval) * time.Second
			}
			// Панель просит обновиться? Ставим ТОЛЬКО подписанное и более новое.
			// Самообновление работает лишь если вшит пубключ (иначе фича выключена).
			// При успехе процесс заменяется через exec и сюда не возвращается.
			// В ОТДЕЛЬНОЙ горутине: на плохом канале докачка идёт кусками с повторами
			// и легко занимает больше 90с, а вотчдог считает такой цикл зависшим и
			// перезапускает агента — обновление не доходило и до половины. Флаг
			// updating страхует от параллельных попыток на каждом отчёте.
			if updatePubKeyB64 != "" && cfg.Update != nil &&
				cfg.Update.Version != "" && cfg.Update.Version != version &&
				updating.CompareAndSwap(false, true) {
				want := cfg.Update.Version
				go func() {
					defer updating.Store(false)
					if uerr := selfUpdate(url, want); uerr != nil {
						fmt.Fprintf(os.Stderr, "kervax-agent: обновление отклонено: %v\n", uerr)
					}
				}()
			}
			// команды из очереди — в отдельных горутинах (не блокируем цикл/вотчдог)
			for _, c := range cfg.DockerCommands {
				go runDockerCommand(url, token, c)
			}
			for _, c := range cfg.KubeCommands {
				go runKubeCommand(url, token, c)
			}
			for _, c := range cfg.BackupCommands {
				go runBackupCommand(url, token, c)
			}
		}
		time.Sleep(interval)
	}
}
