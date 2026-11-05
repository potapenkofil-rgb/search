import asyncio
import json
import sys

from leadhunter.db import DB
from leadhunter.parser import ParserTool, build_args, parse_records, to_companies


def test_build_args_keeps_values_as_single_argv():
    args = build_args("python3 main.py --query {niche} --city {city} --limit {limit}", {"niche": "салон красоты; rm -rf /", "city": "Санкт-Петербург", "limit": 5})
    assert args == ["python3", "main.py", "--query", "салон красоты; rm -rf /", "--city", "Санкт-Петербург", "--limit", "5"]


def test_parse_records_formats():
    js = json.dumps([{"Название": "Шиномонтаж", "Телефон": "+7 1", "Адрес": "ул. 1"}], ensure_ascii=False)
    jl = '{"name": "A", "phone": "1"}\n{"name": "B"}\n'
    wrapped = json.dumps({"items": [{"title": "C"}]})
    csv_text = "name;phone;address\nD;+7 2;ул. 2\nE;+7 3;ул. 3\n"
    assert parse_records(js)[0]["Название"] == "Шиномонтаж"
    assert [r["name"] for r in parse_records(jl)] == ["A", "B"]
    assert parse_records(wrapped)[0]["title"] == "C"
    assert [r["name"] for r in parse_records(csv_text)] == ["D", "E"]
    companies = to_companies(parse_records(js) + parse_records(csv_text), "шиномонтаж", "Казань")
    assert [c.name for c in companies] == ["Шиномонтаж", "D", "E"]
    assert companies[0].phone == "+7 1" and companies[0].website is None and companies[0].provider == "twogis_parser"


def test_parser_tool_runs_command_and_tracks_quota(tmp_path):
    script = tmp_path / "fake_parser.py"
    script.write_text(
        "import json, sys\n"
        "args = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "n = int(args['--limit'])\n"
        "print(json.dumps([{'name': f\"{args['--query']} {i}\", 'phone': f'+7 {i}', 'city': args['--city']} for i in range(n)], ensure_ascii=False))\n",
        encoding="utf-8",
    )
    db = DB(":memory:")
    tool = ParserTool({"enabled": True, "command": f"{sys.executable} {script} --query {{niche}} --city {{city}} --limit {{limit}}", "daily_limit": 7, "per_request_limit": 5}, db)
    first = asyncio.run(tool.run("автосервис", "Казань", 10))
    assert len(first) == 5 and first[0].name == "автосервис 0" and first[0].city == "Казань"
    assert tool.left_today() == 2
    second = asyncio.run(tool.run("автосервис", "Казань", 10))
    assert len(second) == 2 and tool.left_today() == 0
    assert asyncio.run(tool.run("автосервис", "Казань", 10)) == []
