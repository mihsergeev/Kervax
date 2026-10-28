// "Бэкап вышел за окно" для идущего прогона: гоняет backend/tests/test_backup_window_front.py
// (сборка rolldown + node). Печатает JSON: текст уведомления или null по случаям.
// Время - местное, как у backupWindowNote: часы старта и дедлайна от часового пояса не зависят
import type { Server } from '../src/api'
import { backupWindowNote } from '../src/serverUtils'

const at = (d: number, h: number, m = 0) => new Date(2026, 9, d, h, m).getTime()
const srv = (started: number, ended: number) =>
  ({
    backup_deadline_hour: 8,
    last_report: {
      backup: { present: true, configured: true, started_ts: started / 1000, last_backup_ts: ended / 1000 },
    },
  }) as unknown as Server

// вчерашний прогон 27.10 23:30 - 28.10 00:29, сегодняшний начался 28.10 в 23:30
const running = srv(at(28, 23, 30), at(28, 0, 29))

console.log(JSON.stringify({
  just_started: backupWindowNote(running, at(28, 23, 35)),
  past_midnight: backupWindowNote(running, at(29, 3)),
  past_deadline: backupWindowNote(running, at(29, 8, 5)),
  early_start_past_deadline: backupWindowNote(srv(at(29, 2), at(28, 0, 29)), at(29, 9)),
  early_start_in_window: backupWindowNote(srv(at(29, 2), at(28, 0, 29)), at(29, 7)),
}))
