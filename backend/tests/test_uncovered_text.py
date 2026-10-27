"""Текст сводки "Покрытие" в чате: что и где, ссылка туда, где это чинится, и что сделать.

Приходило "Kubernetes без доступа: bk-01" и "kubeexpiry-setup: bk-01" -
сервер дважды, без ссылки и без слова о том, что делать (27.10.2026)."""

from app import collector
from app.models import Server

BASE = "https://kervax.example.com"


def _srv(sid: int, name: str) -> Server:
    return Server(id=sid, name=name, token_hash="x")


def test_one_server_two_gaps_one_line_and_command():
    """Кластер без доступа и helper сроков на одной ноде: сервер один раз, ссылка в Кубер (там
    блок "Нет доступа к kube-api"), одна команда: kube-setup только явной установкой, и тот же
    прогон ставит остальные helper'ы ноды."""
    txt = collector._uncov_short([(_srv(15, "bk-01"), ["kube", "helper:kubeexpiry-setup"])], BASE)
    lines = txt.split("\n")
    assert lines[0] == "🧩 <b>Покрытие</b>"
    assert lines[1] == "<b>Новое без мониторинга</b>"
    assert lines[2] == (f'<a href="{BASE}/?kube=15">bk-01</a>: '
                        "Kubernetes без доступа, нет kubeexpiry-setup")
    assert lines[3] == ("Что сделать: <code>ansible-playbook playbooks/kervax_helpers.yml "
                        "-l 'bk-01' -e kervax_install=kube-setup</code>")
    assert len(lines) == 4, txt
    assert txt.count("bk-01<") == 1


def test_same_gap_on_many_servers_one_line():
    """12 серверов без одного helper'а: одна строка "что: где", имена ссылками в карточку
    сервера (там команда установки), остальные "и еще N", команда одна на всех."""
    rows = [(_srv(i, f"node-{i:02}"), ["helper:webfiles-setup"]) for i in range(1, 13)]
    txt = collector._uncov_short(rows, BASE)
    lines = txt.split("\n")
    assert lines[2].startswith("Нет webfiles-setup: ")
    assert f'<a href="{BASE}/?server=1">node-01</a>' in lines[2]
    assert lines[2].endswith(" и еще 9")
    assert lines[3].startswith("Что сделать: <code>ansible-playbook playbooks/kervax_helpers.yml -l 'node-01,node-02,")
    assert "node-12'</code>" in lines[3]
    assert len(lines) == 4


def test_docker_without_access_points_to_docker_section():
    """Доступ к Docker плейбук не дает: строка ведет в раздел Докер ноды, там команда прокси."""
    txt = collector._uncov_short([(_srv(7, "web-a"), ["docker"])], BASE)
    assert f'<a href="{BASE}/?docker=7">web-a</a>: Docker без доступа' in txt
    assert txt.endswith("Что сделать: включите read-only proxy, команда в разделе Докер по ссылке")
    assert "ansible-playbook" not in txt


def test_mixed_gaps_each_action_once():
    """Разные дыры на разных серверах: строка на каждый набор, действия по одному разу, нода с
    кластером в обычную команду плейбука не попадает (ее закрывает команда с kube-setup)."""
    rows = [
        (_srv(1, "k8s-a"), ["kube"]),
        (_srv(2, "db-a"), ["helper:dbstat-setup"]),
        (_srv(3, "db-b"), ["helper:dbstat-setup"]),
        (_srv(4, "web-a"), ["docker", "helper:webserver-setup"]),
    ]
    txt = collector._uncov_short(rows, BASE)
    lines = txt.split("\n")
    assert lines[2] == f'<a href="{BASE}/?kube=1">k8s-a</a>: Kubernetes без доступа'
    assert lines[3].startswith("Нет dbstat-setup: ") and "?server=2" in lines[3] and "?server=3" in lines[3]
    assert lines[4] == (f'<a href="{BASE}/?docker=4">web-a</a>: Docker без доступа, нет webserver-setup')
    assert lines[5] == "Что сделать:"
    assert lines[6] == ("Доступ в Kubernetes: <code>ansible-playbook playbooks/kervax_helpers.yml -l 'k8s-a' "
                        "-e kervax_install=kube-setup</code>")
    assert lines[7] == "Helper'ы: <code>ansible-playbook playbooks/kervax_helpers.yml -l 'db-a,db-b,web-a'</code>"
    assert lines[8] == "Docker: включите read-only proxy, команда в разделе Докер по ссылке"


def test_more_groups_than_rows_counted_as_servers():
    rows = [(_srv(i, f"n{i}"), [f"helper:h{i}-setup"]) for i in range(1, 6)]
    txt = collector._uncov_short(rows, BASE)
    assert "И еще 2 сервера" in txt
    assert "n4" not in txt.split("Что сделать")[0]


def test_no_panel_url_plain_names():
    txt = collector._uncov_short([(_srv(15, "bk-01"), ["kube"])], "")
    assert "<a " not in txt
    assert "bk-01: Kubernetes без доступа" in txt


def test_chat_text_keyboard_symbols_only():
    """В чат только символы с клавиатуры (формат A): без длинных тире, елочек, стрелок и ё."""
    rows = [(_srv(1, "k8s-a"), ["kube", "helper:kubeexpiry-setup"]), (_srv(4, "web-a"), ["docker"]),
            (_srv(2, "db-a"), ["helper:dbstat-setup"])]
    txt = collector._uncov_short(rows, BASE)
    for ch in "—–«»…→↳·ёЁ":
        assert ch not in txt, ch
