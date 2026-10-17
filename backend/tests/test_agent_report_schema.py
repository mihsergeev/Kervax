"""Поля отчета агента и схема приема (AgentReportIn).

Pydantic молча выбрасывает поля, которых нет в схеме: cpu_groups агент слал с 2.21, а до панели
они не доходили, и алерт cpu_spin две недели молчал без единой ошибки. Тест сверяет теги json
структуры report в agent/main.go со схемой: новое поле агента без поля в схеме не пройдет."""
import re
from pathlib import Path

from app.schemas import AgentReportIn

MAIN_GO = Path(__file__).resolve().parents[2] / "agent" / "main.go"


def _agent_fields() -> set[str]:
    src = MAIN_GO.read_text(encoding="utf-8")
    body = src[src.index("type report struct {"):]
    body = body[:body.index("\n}\n")]
    return {m.group(1) for m in re.finditer(r'`json:"([a-z0-9_]+)[,"]', body)}


def test_every_agent_field_is_in_the_schema():
    fields = _agent_fields()
    assert len(fields) > 50, "структура report в agent/main.go не нашлась"
    missing = sorted(fields - set(AgentReportIn.model_fields))
    assert not missing, f"поля агента, которые схема выбросит: {missing}"


def test_cpu_groups_and_disk_writers_survive():
    groups = [{"comm": "chrome", "kind": "pod", "owner": "pod 56279ecf", "n": 47, "cpu": 4510.4,
               "spin": 45, "spin_age": 3600}]
    writers = [{"kind": "container", "name": "pg", "w": 10**9, "r": 0, "s": 3600}]
    rep = AgentReportIn(hostname="web-a", cpu_groups=groups, disk_writers=writers,
                        disk_writers_v1=True).model_dump()
    assert rep["cpu_groups"] == groups and rep["disk_writers"] == writers and rep["disk_writers_v1"] is True
    # агент старше 2.21: групп нет - None, cpu_spin не оценивается
    old = AgentReportIn(hostname="web-a").model_dump()
    assert old["cpu_groups"] is None and old["disk_writers"] == [] and old["disk_writers_v1"] is False
