"""CronJob кластера: "упал" с причиной и строкой лога или "не запускается" с причиной из событий
(app/kube_cron.py), смена расписания не дает ложного "не отработал", алерт по формату A ведет
прямо на строку CronJob и открывает лог."""
from datetime import datetime, timedelta, timezone

from app import collector, kube_cron, srv_short

from tests.test_home_problems import _srv

NOW = datetime(2026, 10, 23, 12, 51, tzinfo=timezone.utc)
TS = NOW.timestamp()


def _rep(*crons, pods=None):
    return {"clock_unix": TS, "kube": {"access": True, "cronjobs": list(crons), "pods": pods or []}}


def _cj(name="export", **kw):
    return {"ns": "default", "name": name, "image": "registry.example/app:1", "schedule": "0 3 * * *", **kw}


def _why(rep, state=None):
    return collector.cron_job_issues(rep, NOW, False, state)


def test_failed_run_with_cause_and_log_line():
    """Агент прислал разбор упавшего Job: причина и код в строке, строка лога - отдельно для алерта."""
    last = {"job": "export-29879100", "st": "failed", "reason": "BackoffLimitExceeded", "pod": "export-29879100-abcde",
            "pr": "OOMKilled", "x": 137, "log": "PHP Fatal error: Allowed memory size exhausted"}
    rep = _rep(_cj(last_schedule=TS - 8 * 3600, last_success=TS - 32 * 3600, last=last))
    items = collector.cron_job_items(rep, NOW, False)
    assert items["default/export"] == {
        "kind": "fail", "why": "упал: BackoffLimitExceeded, OOMKilled, код 137",
        "detail": "BackoffLimitExceeded, OOMKilled, код 137",
        "log": "PHP Fatal error: Allowed memory size exhausted"}
    # ошибка выхода без своего названия - просто код
    last2 = {"job": "x-1", "st": "failed", "reason": "BackoffLimitExceeded", "pr": "Error", "x": 1}
    assert _why(_rep(_cj(last_schedule=TS - 3600, last_success=TS - 30 * 3600, last=last2))) == {
        "default/export": "упал: BackoffLimitExceeded, код 1"}
    # падает давно: и причина, и сколько нет успешного
    rep = _rep(_cj(last_schedule=TS - 3600, last_success=TS - 3 * 86400, last=last2))
    assert _why(rep) == {"default/export": "упал: BackoffLimitExceeded, код 1, успешного прогона нет 3.0 дн"}
    # старый агент без разбора - как раньше
    assert _why(_rep(_cj(last_schedule=TS - 8 * 3600, last_success=TS - 32 * 3600))) == {
        "default/export": "последний прогон не удался"}


def test_not_starting_reasons_from_events():
    """Запусков нет: причина из свежего события CronJob, старое (до последнего запуска) не в счет."""
    ls = TS - 4.5 * 86400
    ev = {"r": "FailedCreate", "m": "Error creating job: jobs.batch is forbidden: exceeded quota", "t": TS - 3600, "w": True}
    other = _cj("hourly", schedule="0 * * * *", last_schedule=TS - 1800, last_success=TS - 1700)
    assert _why(_rep(_cj(last_schedule=ls, last_success=ls + 600, ev=ev), other)) == {
        "default/export": "не запускается 4.5 дн: Job не создается: jobs.batch is forbidden: exceeded quota"}
    miss = {"r": "MissSchedule", "m": "Missed scheduled time to start a job", "t": TS - 3600, "w": True}
    assert _why(_rep(_cj(last_schedule=ls, last_success=ls + 600, deadline=200, ev=miss), other)) == {
        "default/export": "не запускается 4.5 дн: пропущен запуск, startingDeadlineSeconds 200"}
    many = {"r": "TooManyMissedTimes", "m": "too many missed start times", "t": TS - 3600, "w": True}
    assert _why(_rep(_cj(last_schedule=ls, last_success=ls + 600, ev=many), other))["default/export"] == (
        "не запускается 4.5 дн: пропущено слишком много запусков")
    old = dict(ev, t=ls - 60)
    assert _why(_rep(_cj(last_schedule=ls, last_success=ls + 600, ev=old), other)) == {
        "default/export": "не запускается 4.5 дн"}
    # стоят все CronJob кластера - дело в контроллере
    stuck = _cj("hourly", schedule="0 * * * *", last_schedule=ls, last_success=ls + 60)
    assert _why(_rep(_cj(last_schedule=ls, last_success=ls + 600), stuck))["default/export"] == (
        "не запускается 4.5 дн: контроллер не создает Job: стоят все CronJob кластера")
    # приостановленный обычный - чье-то решение
    assert _why(_rep(_cj(last_schedule=ls, last_success=ls + 600, suspend=True))) == {}


def test_hanging_run_with_forbid_and_pod_reason():
    pods = [{"ns": "default", "name": "export-29870000-xk2pq", "phase": "Pending", "owner": "Job",
             "reason": "ImagePullBackOff"}]
    rep = _rep(_cj(active=1, active_job="export-29870000", policy="Forbid", last_schedule=TS - 3 * 86400,
                   last_success=TS - 4 * 86400), pods=pods)
    assert _why(rep) == {"default/export": "прошлый Job идет 3.0 дн, Forbid не пускает новые, под ImagePullBackOff"}


def test_schedule_change_is_not_a_miss():
    """Раз в неделю -> каждый день: прошлый успех по старому расписанию 4 дня назад - не беда.
    Отсчет от смены расписания; не запустился и по новому - тогда уже "не запускается"."""
    ls = TS - 4.5 * 86400
    weekly = _rep(_cj(schedule="0 3 * * 1", last_schedule=ls, last_success=ls + 5 * 3600))
    state = {kube_cron.STATE_KEY: kube_cron.track(None, weekly, NOW - timedelta(days=3))}
    assert state[kube_cron.STATE_KEY]["default/export"] == {"s": "0 3 * * 1", "at": ""}
    assert _why(weekly, state) == {}
    daily = _rep(_cj(last_schedule=ls, last_success=ls + 5 * 3600))
    state = {kube_cron.STATE_KEY: kube_cron.track(state[kube_cron.STATE_KEY], daily, NOW)}
    assert state[kube_cron.STATE_KEY]["default/export"]["at"] == NOW.isoformat()
    assert _why(daily, state) == {}
    # тот же отчет через три дня: по новому расписанию запусков тоже нет
    later = NOW + timedelta(days=3)
    assert collector.cron_job_issues(daily, later, False, state) == {"default/export": "не запускается 7.5 дн"}
    # без памяти о смене (как было) - ложный алерт сразу
    assert _why(daily) == {"default/export": "не запускается 4.5 дн"}
    # расписание не менялось - отметка остается прежней
    again = kube_cron.track(state[kube_cron.STATE_KEY], daily, later)
    assert again == state[kube_cron.STATE_KEY]


def test_alert_short_text_and_link():
    """Формат A: что случилось, CronJob с причиной, строка лога; ссылка открывает его строку и лог."""
    last = {"job": "export-29879100", "st": "failed", "reason": "BackoffLimitExceeded", "pr": "Error", "x": 1,
            "log": "SQLSTATE[HY000] [2002] Connection refused"}
    rep = _rep(_cj(last_schedule=TS - 3600, last_success=TS - 30 * 3600, last=last))
    s = _srv(NOW, rep)
    lvl, ctx = collector._server_conditions(s, NOW)["kube_cron"]
    assert lvl == 1 and ctx["url_tail"] == "&cj=default/export&log=1"
    what, facts = srv_short.short("kube_cron", ctx, s)
    assert what == "Упал CronJob"
    assert facts == ["default/export - BackoffLimitExceeded, код 1",
                     "Последняя строка лога: SQLSTATE[HY000] [2002] Connection refused"]
    # не запускается: дни словом
    ls = TS - 4.3 * 86400
    rep = _rep(_cj(last_schedule=ls, last_success=ls + 60), _cj("hourly", schedule="0 * * * *",
                                                                  last_schedule=TS - 600, last_success=TS - 500))
    lvl, ctx = collector._server_conditions(_srv(NOW, rep), NOW)["kube_cron"]
    assert srv_short.short("kube_cron", ctx, s) == ("CronJob не запускается", ["default/export - 4 дня"])
    # два CronJob - именами, ссылка на раздел
    rep["kube"]["cronjobs"].append(_cj("report", last_schedule=TS - 3600, last_success=TS - 30 * 3600))
    lvl, ctx = collector._server_conditions(_srv(NOW, rep), NOW)["kube_cron"]
    assert "url_tail" not in ctx
    assert srv_short.short("kube_cron", ctx, s) == ("Не отрабатывают CronJob", ["default/export, default/report"])
    # контекст до обновления (алерт висел до выкатки) - как раньше
    assert srv_short.short("kube_cron", {"jobs": "a/b (нет успешного прогона 3.0 дн)", "n": 1}, s) == (
        "Не отработал CronJob", ["a/b"])


def test_job_removed_by_history_keeps_known_cause():
    """Job упавшего запуска удален по истории: причина та, что агент запомнил."""
    last = {"job": "export-29879100", "st": "failed", "reason": "DeadlineExceeded", "gone": True}
    rep = _rep(_cj(last_schedule=TS - 3600, last_success=TS - 30 * 3600, last=last, fail_keep=0))
    assert _why(rep) == {"default/export": "упал: DeadlineExceeded"}
