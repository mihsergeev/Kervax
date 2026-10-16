"""Что именно было в алерте (app/alert_what.py): конкретный объект и коротко почему."""
from app.alert_what import items, key, merge_notes


def test_unit_with_log_line():
    t = "⚠️⚙️ web-a — упал app-sync.service: код 1, 11 мин назад\n↳ app-sync.service: ModuleNotFoundError: No module named 'requests'"
    assert items("units", t) == [("упал app-sync.service", "код 1: ModuleNotFoundError: No module named 'requests'")]


def test_several_units():
    t = "⚠️⚙️ web-a — упали юниты: a.service (код 1), b.service (не уложился во время) и еще 2"
    assert items("units", t) == [("упал a.service", "код 1"), ("упал b.service", "не уложился во время")]


def test_pod_restarts_drop_replicaset_hash():
    t = ("🔄☸️ k8s-a — под shop/worker-7fb484cbcb-wvkcj перезапускался 5 раз за сутки, последний 0 мин назад; "
         "под shop/worker-7fb484cbcb-xmszd перезапускался 6 раз за сутки; под shop/cron-sync-795d8cc8c-fttx8 "
         "перезапускался 5 раз за сутки")
    assert [w for w, _ in items("kube_restarts", t)] == ["перезапускается под shop/worker", "перезапускается под shop/cron-sync"]


def test_objects_with_reasons_become_rows():
    t = ("💾 backup-a — репозитории требуют внимания: 2 шт.: web-a (нет новых бэкапов 6 дн), "
         "web-b (бэкап опоздал: обычно раз в сутки, последний 30 ч назад)")
    assert items("backup_repo", t) == [
        ("репозитории требуют внимания: web-a", "нет новых бэкапов 6 дн"),
        ("репозитории требуют внимания: web-b", "бэкап опоздал: обычно раз в сутки"),
    ]
    t = ("💾 k8s-a — дамп-CronJob не отрабатывает (приостановлен / прогон упал / давно нет успешного дампа): "
         "db/pg-backup (последний прогон не удался)")
    assert items("backup_cron", t) == [("дамп-CronJob не отрабатывает: db/pg-backup", "последний прогон не удался")]


def test_explanation_tail_dropped():
    t = ("🔑🐳 web-a — у контейнеров ключи от хоста: agent-1-1 (privileged), agent-2-1 (privileged) - "
         "взлом приложения в них даст root на сервере")
    assert items("docker_risk", t) == [
        ("у контейнеров ключи от хоста: agent-1-1", "privileged"),
        ("у контейнеров ключи от хоста: agent-2-1", "privileged"),
    ]


def test_threshold_and_peak():
    t = "⚠️ web-a — CPU 96% ≥ 90% - больше всего CPU у ffmpeg 1235%, prometheus 121%, nginx 96%"
    assert items("cpu", t) == [("CPU выше порога", "96%: ffmpeg, prometheus, nginx")]
    # "ходит вокруг порога" - та же строка, что и сам порог
    flap = items("cpu", "⚠️ web-a — CPU ходит вокруг порога: 3 срабатывания за 6 ч - дальше молчу")
    assert key(flap[0][0]) == key(items("cpu", t)[0][0])
    assert merge_notes(["96%: ffmpeg, nginx", "100%: php, ffmpeg", "", "94%: ffmpeg"]) == "до 100%: ffmpeg, nginx, php"
    assert merge_notes(["код 1", "не уложился во время"]) == "не уложился во время"


def test_pressure_and_ports():
    t = "🫁 web-a — диск не успевает: все процессы разом ждут диск 57% времени - sda занят на 89%, задержка 19 мс"
    assert items("pressure", t) == [("диск sda не успевает", "ждут диск 57% времени")]
    t = "🫁 web-a — зависли в ожидании диска или сети 9 процессов (D-state): postgres (7), gzip (1), systemd (1)"
    assert items("pressure", t) == [("процессы зависли в D-state", "postgres, gzip, systemd")]
    t = "🔓 web-a — открыто в интернет: etcd 2380 (процесс пользователя etcd) на 203.0.113.5; kubelet 10250 на 203.0.113.5"
    assert items("exposed", t) == [("открыт в интернет etcd 2380", "процесс пользователя etcd"),
                                   ("открыт в интернет kubelet 10250", "")]


def test_colon_inside_parens_and_monitor_tail():
    t = "🔏 web-a — сертификат example.com, www.example.com истек 231 дн назад (на хосте: /etc/ssl/x.pem), сайты на нем уже отдают ошибку"
    assert items("web_cert", t) == [("сертификат example.com, www.example.com", "истек")]
    t = "🔴 shop.example.com — сервер не ответил на попытку соединения (ConnectTimeout) · монитор\n↳ и api.example.com"
    assert items("down", t) == [("сервер не ответил на попытку соединения", "ConnectTimeout")]


def test_key_ignores_numbers_but_not_names():
    assert key("диск выше 85%") == key("диск выше 90%")
    assert key("у контейнеров ключи от хоста: agent-1-1") != key("у контейнеров ключи от хоста: agent-2-1")
