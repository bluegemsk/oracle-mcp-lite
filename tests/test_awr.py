from pathlib import Path
from oracle_mcp_lite import awr
F = Path(__file__).parent / "fixtures"

def _check(r):
    assert r["top_waits"][0]["event"].lower() in ("db cpu", "log file sync")
    events = [w["event"] for w in r["top_waits"]]
    assert "log file sync" in events and "db file sequential read" in events
    assert r["top_sql"][0]["sql_id"] == "7h35uxf5uhmm1"
    assert r["top_sql"][0]["pct_total"] == 34.7
    assert r["elapsed_min"] == 60.02 and r["db_time_min"] == 240.1
    assert r["avg_active_sessions"] == 4.0
    adv = " ".join(x["advice"] for x in r["recommendations"])
    assert "Commit overhead" in adv and "bind variables" in adv and "DBMS_XPLAN" in adv

def test_html():
    r = awr.analyze((F / "awr_sample.html").read_text())
    assert r["format"] == "html" and r["db"]["db_name"] == "PRODDB"
    assert r["load_profile"]["Hard parses (SQL)"] == 250.3
    assert r["top_waits"][1]["waits"] == 2_500_000
    _check(r)
    assert any("execution count" in x["advice"] for x in r["recommendations"])

def test_text():
    r = awr.analyze((F / "awr_sample.txt").read_text())
    assert r["format"] == "text" and r["db"]["db_name"] == "PRODDB"
    assert r["top_waits"][1]["waits"] == 9.5e6
    _check(r)
    assert r["top_sql"][0]["module"] == "JDBC Thin Client"
    assert r["top_sql"][0]["text"].startswith("SELECT")

def test_num():
    assert awr._num("1,234.5") == 1234.5 and awr._num("2K") == 2000 and awr._num("") is None


def test_10g_titleless_tables():
    r = awr.analyze((F / "awr_10g.html").read_text())
    assert r["report_type"] == "awr" and r["db"]["version"] == "10gR2" and r["db"]["db_name"] == "SHOPDB"
    assert r["elapsed_min"] == 600 and r["db_time_min"] == 1200 and r["avg_active_sessions"] == 2.0
    assert [w["event"] for w in r["top_waits"]][:3] == ["ARCH wait on SENDREQ", "CPU time", "db file scattered read"]
    assert r["top_waits"][0]["pct_db_time"] == 41.7
    assert {k: v for k, v in r["top_sql"][0].items() if k != "advice"} == {"sql_id": "aaaaaaaaaaaa1", "elapsed_s": 18000, "executions": 10, "pct_total": 25.0,
                               "cpu_pct": None, "io_pct": None, "module": "batch.exe", "text": "select ... from orders"}
    adv = " ".join(x["advice"] for x in r["recommendations"])
    assert "Data Guard" in adv and "Full scans" in adv and "connection pool" in adv and "DBMS_XPLAN" in adv


def test_global_rac():
    r = awr.analyze((F / "awr_global_rac.html").read_text())
    assert r["report_type"] == "awr_global_rac" and r["db"]["db_name"] == "SHOPDB" and r["db"]["version"] == "19c"
    assert [i["instance"] for i in r["instances"]] == ["SHOP1", "SHOP2"]
    assert r["db_time_min"] == 1200 and r["avg_active_sessions"] == 5.0
    assert [w["event"] for w in r["top_waits"]] == ["DB CPU", "gc cr block busy", "gcs log flush sync"]
    assert r["instances"][1]["top_waits"][0]["event"] == "gc cr block busy"
    assert "services" in r["instances"][1]["top_waits"][0]["advice"]
    assert r["top_sql"][0]["elapsed_s"] == 21600 and r["top_sql"][0]["pct_total"] == 30.0  # first "Elapsed (s)" column
    fx = " ".join(x["finding"] for x in r["recommendations"])
    assert "RAC load imbalance" in fx and "gc cr block busy" in fx


def test_ash_rac():
    r = awr.analyze((F / "ash_rac.html").read_text())
    assert r["report_type"] == "ash_rac" and r["db"]["release"] == "19.0.0.0.0"
    assert r["avg_active_sessions"] == 6.0 and r["elapsed_min"] == 60 and r["ash"]["sample_count"] == 3600
    assert r["top_waits"][0]["event"] == "CPU + Wait for CPU" and r["top_waits"][0]["pct_db_time"] == 55
    assert [s["sql_id"] for s in r["top_sql"]] == ["cccccccccccc1", "cccccccccccc2"]
    assert r["recommendations"][0]["finding"] == "ASH report detected"
    assert any("Row lock" in x["advice"] for x in r["recommendations"])



def test_advice_and_summary():
    r = awr.analyze((F / "awr_10g.html").read_text())
    assert all(w["advice"] for w in r["top_waits"]) and all(s["advice"] for s in r["top_sql"])
    arch = r["top_waits"][0]["advice"]
    assert "standby" in arch and "compression" not in arch  # 11g+ tip not offered on 10g
    assert "1,800.0 s/exec" in r["top_sql"][0]["advice"] and "SQL Profile" in r["top_sql"][0]["advice"]
    assert len(r["tuning_summary"]) == 3 and r["tuning_summary"][0].startswith("1. ARCH wait on SENDREQ")
    g = awr.analyze((F / "awr_global_rac.html").read_text())
    assert "plan baseline" in g["top_sql"][0]["advice"]


def test_wait_kb():
    from oracle_mcp_lite.advice import wait_advice, GENERIC_WAIT
    assert "batch commits" in wait_advice("log file sync", ver=19) and "adaptive" in wait_advice("log file sync", ver=19)
    assert "adaptive" not in wait_advice("log file sync", ver=10)
    assert "unindexed foreign key" in wait_advice("enq: TM - contention")
    assert wait_advice("some brand new event") == GENERIC_WAIT
