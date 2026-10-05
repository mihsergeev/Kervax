"""Поды Kubernetes в алерте по памяти (агент 2.16+ отдает память подов своей ноды)."""
from app.collector import cause_text

GB = 1024 ** 3


def test_memory_cause_names_pods_of_this_node():
    rep = {"top_mem": [], "kube": {"pods": [
        {"ns": "access-hub", "name": "access-hub-5965758d6c-tbvbr", "mem": 3 * GB,
         "ctrl": "deployment/access-hub"},
        {"ns": "default", "name": "postgres-0", "mem": 5 * GB, "ctrl": "statefulset/postgres"},
        {"ns": "default", "name": "small-1", "mem": GB // 2},
        # под другой ноды: памяти агент не знает, в хвост не попадает
        {"ns": "default", "name": "elsewhere-1"},
    ]}}
    assert cause_text(rep, "mem", {}) == (
        " - поды: default/postgres-0 5.0 ГБ, access-hub/access-hub-5965758d6c-tbvbr 3.0 ГБ")


def test_memory_cause_without_pods_is_unchanged():
    assert cause_text({"top_mem": [], "kube": {"pods": [{"ns": "a", "name": "b"}]}}, "mem", {}) == ""
