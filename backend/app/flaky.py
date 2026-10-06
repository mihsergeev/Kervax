"""Сайт отвечает через раз.

Обычный алерт ждет N неудач подряд (Check.alert_after_failures). Он гасит одиночные блипы, но
слеп к частичной поломке: когда из трех подов зависает один, падает каждый третий запрос, сбои
идут вперемешку с успешными проверками, и трех подряд почти не бывает. Один сайт так
висел каждый день по 30-50 минут, доступность за сутки была 93%, а алертов не было ни одного.

Здесь второе условие: несколько коротких серий сбоев среди последних проверок. Пороги подобраны
на неделе реальных проверок трех панелей (514 мониторов, 06.10.2026): 4 сбоя тремя сериями из 20
сработали 8 раз и только на этом сайте; на шаг мягче (3 сбоя двумя сериями) уже ловит случайные
одиночные сбои у других сайтов.
"""

WINDOW = 20  # последних проверок
FAILS = 4    # не меньше стольких неудач в окне
RUNS = 3     # и хотя бы тремя отдельными сериями: вперемешку с успешными, а не одним падением
CLEAR = 1    # отбой, когда в окне осталось не больше стольких неудач


def short_failures(statuses: list[str], long_run: int) -> tuple[int, int]:
    """Неудачи в последних WINDOW проверках, которые обычный алерт не видит: (сколько, серий).

    statuses - статусы от старых к новым, лучше на long_run штук длиннее окна: серия, которая
    начинается до окна, могла быть длинной. Серии длиной от long_run - это обычное падение, его
    ловит алерт "N неудач подряд", здесь их не считаем. degraded (медленно) - не неудача."""
    long_run = max(long_run, 1)
    runs: list[tuple[int, int]] = []  # (индекс за концом серии, длина) по всей истории
    n = 0
    for i, st in enumerate(statuses):
        if st == "down":
            n += 1
        elif n:
            runs.append((i, n))
            n = 0
    if n:
        runs.append((len(statuses), n))
    lo = max(len(statuses) - WINDOW, 0)
    fails = count = 0
    for end, length in runs:
        if length >= long_run:
            continue
        inside = end - max(end - length, lo)  # сколько неудач серии попало в окно
        if inside > 0:
            fails += inside
            count += 1
    return fails, count


def in_outage(statuses: list[str], long_run: int) -> bool:
    """Идет обычное падение: последние long_run проверок подряд не прошли."""
    long_run = max(long_run, 1)
    tail = statuses[-long_run:]
    return len(tail) == long_run and all(s == "down" for s in tail)


def is_flaky(statuses: list[str], long_run: int, was: bool) -> bool:
    """Отвечает ли сайт через раз после очередной проверки (с гистерезисом).

    Входим при FAILS неудачах RUNS сериями, выходим, когда неудач осталось не больше CLEAR. Пока
    идет обычное падение, состояние не меняем: о падении пишет обычный алерт, а частичная поломка,
    если она есть, снова будет видна после восстановления."""
    if in_outage(statuses, long_run):
        return was
    fails, count = short_failures(statuses, long_run)
    if not was:
        return fails >= FAILS and count >= RUNS
    return fails > CLEAR
