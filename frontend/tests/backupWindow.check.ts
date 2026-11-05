// "Бэкап вышел за окно" для идущих и завершённых прогонов: гоняет backend/tests/test_backup_window_front.py
// (сборка rolldown + node). Печатает JSON: текст уведомления или null по случаям.
// Время - местное, как у backupWindowNote: часы старта и дедлайна от часового пояса не зависят
import type { BackupInfo, Server } from '../src/api'
import { backupWindowNote } from '../src/serverUtils'

const at = (d: number, h: number, m = 0) => new Date(2026, 9, d, h, m).getTime()
const srv = (started: number, ended: number, backup: Partial<BackupInfo> = {}) =>
  ({
    backup_deadline_hour: 8,
    last_report: {
      backup: { present: true, configured: true, started_ts: started / 1000, last_backup_ts: ended / 1000, ...backup },
    },
  }) as unknown as Server

// вчерашний прогон 27.10 23:30 - 28.10 00:29, сегодняшний начался 28.10 в 23:30
const running = srv(at(28, 23, 30), at(28, 0, 29))
// Result=success также встречается во время нового запуска, когда метрика ещё со старого прогона.
const successfulMetric = { metric_present: true, success: 1, service_result: 'success', duration_sec: 2 }
// Старт из systemd оказался на несколько секунд позже метрики завершившегося restic.
const finished = { ...successfulMetric, full_duration_sec: 3 }

console.log(JSON.stringify({
  just_started: backupWindowNote(running, at(28, 23, 35)),
  past_midnight: backupWindowNote(running, at(29, 3)),
  past_deadline: backupWindowNote(running, at(29, 8, 5)),
  early_start_past_deadline: backupWindowNote(srv(at(29, 2), at(28, 0, 29), successfulMetric), at(29, 9)),
  early_start_in_window: backupWindowNote(srv(at(29, 2), at(28, 0, 29)), at(29, 7)),
  finished_in_window_with_skew: backupWindowNote(srv(at(29, 5) + 6000, at(29, 5), finished), at(29, 9)),
  finished_in_window_with_old_late_metric: backupWindowNote(srv(at(29, 5), at(28, 8, 5), finished), at(29, 9)),
  finished_past_deadline: backupWindowNote(srv(at(29, 7, 59), at(29, 8) + 1000, finished), at(29, 9)),
  finished_past_deadline_with_old_early_metric: backupWindowNote(srv(at(29, 7), at(28, 5), {
    ...finished, full_duration_sec: 3900,
  }), at(29, 9)),
  finished_after_midnight_in_window: backupWindowNote(srv(at(28, 23, 30), at(29, 1)), at(29, 9)),
  finished_after_midnight_past_deadline: backupWindowNote(srv(at(28, 23, 30), at(29, 8, 5)), at(29, 9)),
}))
