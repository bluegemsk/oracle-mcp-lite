"""AWR report parser (HTML and text) with rule-based recommendations."""
from __future__ import annotations
import re
from html.parser import HTMLParser


def _num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    m = re.match(r"^-?\d+(\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    rest = s[m.end():].strip().upper()
    mult = {"K": 1e3, "M": 1e6, "G": 1e9}.get(rest[:1], 1)
    return v * mult


class _Tables(HTMLParser):
    """Collect tables as (summary/preceding heading, rows)."""
    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell, self._in_cell = [], None, [], False
        self._heading, self._buf, self._cur = "", [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._cur = {"title": (a.get("summary") or self._heading).strip(), "rows": []}
        elif tag == "tr" and self._cur is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._in_cell, self._cell = True, []
        elif tag in ("h2", "h3", "p", "a"):
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in_cell = False
        elif tag == "tr" and self._row is not None and self._cur is not None:
            if self._row:
                self._cur["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._cur is not None:
            self.tables.append(self._cur)
            self._cur = None
        elif tag in ("h2", "h3", "p", "a"):
            t = " ".join("".join(self._buf).split())
            if t:
                self._heading = t

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)
        else:
            self._buf.append(data)


def _n(s):
    return re.sub(r"[\s_]+", "", (s or "").lower())


def _find(tables, *keys):
    for t in tables:
        title = t["title"].lower()
        if all(k in title for k in keys):
            return t
    return None


def _find_hdr(tables, *cols, title=None, exclude=()):
    """Find a table by its header cells (robust across 10g..23ai and title-less tables).
    Looks at the first 3 rows (RAC/global reports use 2-row headers)."""
    want = [_n(c) for c in cols]
    for t in tables:
        tl = t["title"].lower()
        if title and title not in tl:
            continue
        if any(x in tl for x in exclude):
            continue
        for i, row in enumerate(t["rows"][:3]):
            cells = [_n(c) for c in row]
            if all(any(w in c for c in cells) for w in want):
                return {"title": t["title"], "rows": t["rows"][i:]}
    return None


def _rows_as_dicts(table):
    if not table or len(table["rows"]) < 2:
        return []
    hdr, seen = [], {}
    for h in table["rows"][0]:
        h = h.strip()
        seen[h] = seen.get(h, 0) + 1
        hdr.append(h if seen[h] == 1 else f"{h}#{seen[h]}")  # keep first of duplicate column names
    return [dict(zip(hdr, r)) for r in table["rows"][1:] if len(r) == len(hdr)]


def _get(d, *names):
    """Value of the first column whose normalised name equals, else contains, one of names."""
    nn = [_n(x) for x in names]
    for n in nn:
        for k, v in d.items():
            if _n(k) == n:
                return v
    for n in nn:
        for k, v in d.items():
            if n in _n(k):
                return v
    return None


def _major(release):
    m = re.match(r"(\d+)\.(\d+)", release or "")
    if not m:
        return None
    maj, mi = int(m.group(1)), int(m.group(2))
    names = {10: "10g", 11: "11g", 12: "12c", 18: "18c", 19: "19c", 21: "21c", 23: "23ai"}
    return names.get(maj, str(maj)) + ("R2" if maj in (10, 11, 12) and mi == 2 else "")


def _report_type(html):
    head = html[:20000]
    if re.search(r"<title>\s*ASH RAC Report", head, re.I):
        return "ash_rac"
    if re.search(r"<title>\s*ASH Report", head, re.I):
        return "ash"
    if re.search(r"WORKLOAD REPOSITORY REPORT \(RAC\)|<title>\s*AWR RAC Report", head, re.I):
        return "awr_global_rac"
    if re.search(r"<title>\s*AWR Compare|WORKLOAD REPOSITORY COMPARE", head, re.I):
        return "awr_diff"
    return "awr"


def _snap(t, out):
    for r in _rows_as_dicts(_find_hdr(t, "Snap Id", "Snap Time")):
        vals = list(r.values())
        label = vals[0].lower()
        v = next((_num(x) for x in vals[1:] if _num(x) is not None), None)
        if label.startswith("elapsed"):
            out["elapsed_min"] = v
        elif label.startswith("db time"):
            out["db_time_min"] = v
        elif label.startswith("begin snap"):
            out["begin_snap"], out["begin_time"] = _num(vals[1]), vals[2]
        elif label.startswith("end snap"):
            out["end_snap"], out["end_time"] = _num(vals[1]), vals[2]


def _wait_rows(table):
    res = []
    for r in _rows_as_dicts(table):
        ev = _get(r, "event")
        if not ev:
            continue
        res.append({"event": ev, "waits": _num(_get(r, "waits")),
                    "time_s": _num(_get(r, "total wait time (sec)", "total wait time (s)", "time(s)", "total(s)", "time")),
                    "pct_db_time": _num(_get(r, "% db time", "%db time", "% total call time", "% db")),
                    "wait_class": _get(r, "wait class", "class")})
    return res


def _sql_rows(table):
    res = []
    for r in _rows_as_dicts(table):
        sid = _get(r, "sql id")
        if not sid or not re.fullmatch(r"[0-9a-z]{13}", sid.strip()):
            continue
        res.append({"sql_id": sid.strip(),
                    "elapsed_s": _num(_get(r, "elapsed time (s)", "elapsed time", "elapsed (s)")),
                    "executions": _num(_get(r, "executions", "execs")),
                    "pct_total": _num(_get(r, "%total", "% total db time", "% total")),
                    "cpu_pct": _num(_get(r, "%cpu")), "io_pct": _num(_get(r, "%io")),
                    "module": _get(r, "sql module", "module"), "text": _get(r, "sql text")})
    return res


def _parse_awr(t, out):
    dbt = _find_hdr(t, "DB Name", "Release") or _find_hdr(t, "DB Name") or _find(t, "database instance")
    for r in _rows_as_dicts(dbt)[:1]:
        out["db"] = {"db_name": _get(r, "db name"), "instance": _get(r, "instance"),
                     "release": _get(r, "release"), "rac": _get(r, "rac")}
    if not out["db"].get("instance"):
        it = _find_hdr(t, "Instance", "Inst Num", "Startup Time")
        for r in _rows_as_dicts(it)[:1]:
            out["db"]["instance"] = _get(r, "instance")
    hp = _find_hdr(t, "Host Name", "Platform")
    for r in _rows_as_dicts(hp)[:1]:
        out["db"]["platform"], out["db"]["cpus"] = _get(r, "platform"), _num(_get(r, "cpus"))
    _snap(t, out)
    lp = _find(t, "load profile") or _find_hdr(t, "Per Second", "Per Transaction")
    for r in _rows_as_dicts(lp):
        vals = list(r.values())
        if vals and vals[0]:
            out["load_profile"][vals[0].rstrip(":").strip()] = _num(vals[1])
    tw = (_find_hdr(t, "Event", "Waits", "% DB time", exclude=("background", "foreground wait events"))
          or _find_hdr(t, "Event", "Waits", "% Total Call Time"))
    out["top_waits"] = _wait_rows(tw)
    ts = _find_hdr(t, "Elapsed Time", "SQL Id", title="elapsed") or _find_hdr(t, "Elapsed Time", "Executions", "SQL Id")
    out["top_sql"] = _sql_rows(ts)


def _parse_global(t, out):
    ds = _find_hdr(t, "Id", "Name", "RAC", title="database summary")
    for r in _rows_as_dicts(ds)[:1]:
        out["db"] = {"db_name": _get(r, "name"), "rac": _get(r, "rac"), "instance": "*"}
    inst = []
    for r in _rows_as_dicts(_find_hdr(t, "I#", "Instance", "Release", "DB time")):
        inst.append({"inst": _get(r, "i#"), "instance": _get(r, "instance"), "release": _get(r, "release"),
                     "elapsed_min": _num(_get(r, "elapsed time")), "db_time_min": _num(_get(r, "db time"))})
    out["instances"] = inst
    if inst:
        out["db"]["release"] = inst[0]["release"]
        out["elapsed_min"] = max((i["elapsed_min"] or 0) for i in inst) or None
        out["db_time_min"] = sum((i["db_time_min"] or 0) for i in inst) or None
        for i in inst:
            if i["elapsed_min"] and i["db_time_min"] is not None:
                i["avg_active_sessions"] = round(i["db_time_min"] / i["elapsed_min"], 2)
    tw = _find_hdr(t, "I#", "Event", "Waits", "%DB time", title="top timed events")
    rows = _rows_as_dicts(tw)
    per, cluster, cur = {}, [], None
    for r in rows:  # I# may be repeated on every row or only on the first row of each instance group
        i = (r.get("I#") or "").strip()
        if i:
            cur = i
        if cur == "*":
            cluster.append(list(r.values()))
        elif cur and _get(r, "event"):
            per.setdefault(cur, []).append({"event": _get(r, "event"), "pct_db_time": _num(_get(r, "%db time"))})
    out["top_waits"] = _wait_rows({"rows": [tw["rows"][0]] + cluster}) if tw else []
    for i in inst:
        i["top_waits"] = per.get(str(i["inst"]), [])[:5]
    out["top_sql"] = _sql_rows(_find_hdr(t, "SQL Id", "Elapsed (s)", title="elapsed time (global)"))
    for s in out["top_sql"]:
        s["pct_total"] = None  # global table gives per-instance-sum percentages in other columns
    tot = (out.get("db_time_min") or 0) * 60
    if tot:
        for s in out["top_sql"]:
            if s["elapsed_s"] is not None:
                s["pct_total"] = round(100 * s["elapsed_s"] / tot, 2)


def _parse_ash(t, out, html):
    out["ash"] = {}
    db = _find_hdr(t, "DB Name", "Instance") or _find_hdr(t, "Id", "Name", "RAC", title="database summary")
    for r in _rows_as_dicts(db)[:1]:
        out["db"] = {"db_name": _get(r, "db name", "name"), "instance": _get(r, "instance"),
                     "release": _get(r, "release"), "rac": _get(r, "rac")}
    inst = []
    for r in _rows_as_dicts(_find_hdr(t, "I#", "Instance", "% Activity", "Avg Active Sessions")):
        inst.append({"inst": _get(r, "i#"), "instance": _get(r, "instance"), "release": _get(r, "release"),
                     "pct_activity": _num(_get(r, "% activity")), "avg_active_sessions": _num(_get(r, "avg active"))})
    if inst:
        out["instances"] = inst
        out["db"]["release"] = out["db"].get("release") or inst[0]["release"]
    for r in _rows_as_dicts(_find_hdr(t, "Sample Time", "Data Source")):
        vals = list(r.values())
        lab = vals[0].lower()
        if lab.startswith("elapsed"):
            out["elapsed_min"] = _num(vals[1])
        elif lab.startswith("sample count"):
            out["ash"]["sample_count"] = _num(vals[1])
        elif lab.startswith("average active"):
            out["avg_active_sessions"] = _num(vals[1])
        elif lab.startswith("analysis begin"):
            out["begin_time"] = vals[1]
        elif lab.startswith("analysis end"):
            out["end_time"] = vals[1]
    ev = _find_hdr(t, "Event", "Event Class", "% Event", "Avg Active Sessions")
    out["top_waits"] = [{"event": _get(r, "event"), "wait_class": _get(r, "event class"),
                         "pct_db_time": _num(_get(r, "% event")), "aas": _num(_get(r, "avg active")),
                         "waits": None, "time_s": None} for r in _rows_as_dicts(ev)]
    bg = _find_hdr(t, "Event", "Event Class", "% Activity", title="background")
    out["ash"]["top_background_events"] = [{"event": _get(r, "event"), "pct_activity": _num(_get(r, "% activity"))}
                                           for r in _rows_as_dicts(bg)]
    sq = _find_hdr(t, "SQL ID", "% Activity", "Event", title="top sql with top events")
    seen = {}
    for r in _rows_as_dicts(sq):
        sid = (_get(r, "sql id") or "").strip()
        if re.fullmatch(r"[0-9a-z]{13}", sid) and sid not in seen:
            seen[sid] = {"sql_id": sid, "pct_activity": _num(_get(r, "% activity")),
                         "top_event": _get(r, "event"), "elapsed_s": None, "executions": None,
                         "pct_total": _num(_get(r, "% activity")), "module": None, "text": None}
    out["top_sql"] = list(seen.values())
    bl = _find_hdr(t, "Blocking Sid", "% Activity", "Event Caused")
    out["ash"]["top_blocking_sessions"] = [{"pct_activity": _num(_get(r, "% activity")),
                                            "event_caused": _get(r, "event caused")} for r in _rows_as_dicts(bl)][:5]
    ob = _find_hdr(t, "Object ID", "% Activity", "Event")
    out["ash"]["top_objects_pct"] = [{"pct_activity": _num(_get(r, "% activity")), "event": _get(r, "event")}
                                     for r in _rows_as_dicts(ob)][:5]


def parse_html(html: str) -> dict:
    p = _Tables()
    p.feed(html)
    t = p.tables
    rt = _report_type(html)
    out = {"format": "html", "report_type": rt, "db": {}, "load_profile": {}, "top_waits": [], "top_sql": []}
    if rt == "awr_global_rac":
        _parse_global(t, out)
    elif rt in ("ash", "ash_rac"):
        _parse_ash(t, out, html)
    else:
        _parse_awr(t, out)
    out["db"]["version"] = _major(out["db"].get("release"))
    return out


_WAIT_RE = re.compile(r"^(?P<event>[A-Za-z][\w :\-/()*.]+?)\s{2,}(?P<waits>[\d,.]+[KMG]?)\s+"
                      r"(?P<time>[\d,.]+[KMG]?)\s+(?P<avg>[\d,.]+\w*)\s+(?P<pct>[\d.]+)\s+(?P<cls>[\w /]+)$")
_SQL_RE = re.compile(r"^\s*(?P<el>[\d,.]+)\s+(?P<ex>[\d,]+)\s+[\d,.]+\s+(?P<pct>[\d.]+)\s+.*?(?P<id>[0-9a-z]{13})\s*(?P<rest>.*)$")


def parse_text(txt: str) -> dict:
    out = {"format": "text", "report_type": "ash" if re.search(r"^\s*ASH Report", txt[:5000], re.M | re.I) else "awr", "db": {}, "load_profile": {}, "top_waits": [], "top_sql": []}
    lines = txt.splitlines()
    section = None
    for ln in lines:
        s = ln.strip()
        low = s.lower()
        if low.startswith("load profile"):
            section = "lp"; continue
        if low.startswith("top") and "event" in low:
            section = "tw"; continue
        if low.startswith("sql ordered by elapsed"):
            section = "sql"; continue
        if low.startswith(("instance efficiency", "sql ordered by cpu", "wait classes",
                           "time model", "sql ordered by gets")):
            section = None; continue
        m = re.match(r"^\s*Elapsed:\s+([\d,.]+)", ln)
        if m: out["elapsed_min"] = _num(m.group(1))
        m = re.match(r"^\s*DB Time:\s+([\d,.]+)", ln)
        if m: out["db_time_min"] = _num(m.group(1))
        m = re.match(r"^DB Name\s", ln)
        if m:
            idx = lines.index(ln)
            if idx + 2 < len(lines):
                parts = lines[idx + 2].split()
                if parts:
                    out["db"]["db_name"] = parts[0]
        if section == "lp":
            m = re.match(r"^\s*([A-Za-z][\w ()/\-]+?):\s+([\d,.]+)", ln)
            if m: out["load_profile"][m.group(1).strip()] = _num(m.group(2))
        elif section == "tw":
            m = _WAIT_RE.match(s)
            if m:
                out["top_waits"].append({"event": m["event"].strip(), "waits": _num(m["waits"]),
                                         "time_s": _num(m["time"]), "pct_db_time": _num(m["pct"]),
                                         "wait_class": m["cls"].strip()})
        elif section == "sql":
            if out["top_sql"] and s.startswith("Module:"):
                out["top_sql"][-1]["module"] = s[7:].strip(); continue
            if out["top_sql"] and s and not out["top_sql"][-1]["text"] and not _SQL_RE.match(ln) \
                    and not set(s) <= set("-~ ") and out["top_sql"][-1]["module"] is not None:
                out["top_sql"][-1]["text"] = s; continue
            m = _SQL_RE.match(ln)
            if m:
                out["top_sql"].append({"sql_id": m["id"], "elapsed_s": _num(m["el"]),
                                       "executions": _num(m["ex"]), "pct_total": _num(m["pct"]),
                                       "module": None, "text": m["rest"].strip() or None})
    return out


RULES = [
    (("db file sequential read",), "User I/O",
     "High single-block reads: review index access paths of top SQL, check buffer cache size, and storage latency."),
    (("db file scattered read", "direct path read"), "User I/O",
     "Full scans dominate: check missing indexes, stale statistics, or consider partitioning / parallel query."),
    (("log file sync",), "Commit",
     "Commit overhead: reduce commit frequency (commit in loops?), check redo log storage latency, consider batching."),
    (("enq: tx - row lock contention",), "Application",
     "Row lock contention: application serialisation on hot rows; review transaction design and locking order."),
    (("latch: shared pool", "library cache", "cursor: pin s wait on x", "hard parse"), "Concurrency",
     "Parsing contention: use bind variables, check cursor_sharing and shared pool sizing."),
    (("buffer busy waits", "gc buffer busy"), "Concurrency",
     "Hot blocks: consider reverse-key / hash-partitioned indexes or ASSM; on RAC check service affinity."),
    (("log buffer space",), "Configuration", "Increase log_buffer and check redo I/O throughput."),
    (("free buffer waits",), "Configuration", "DBWR cannot keep up: check I/O, db_writer_processes, buffer cache size."),
    (("resmgr:cpu quantum",), "Scheduler", "Resource Manager throttling CPU: review the active resource plan."),
    (("gc cr block busy", "gc cr multi block", "gc current block busy", "gc cr block 2-way", "gc current block 2-way",
      "gc cr grant", "gc current grant"), "Cluster",
     "RAC interconnect / block shipping: the same blocks are used on several instances. Use service-based workload "
     "affinity, check top SQL by cluster wait time and the interconnect latency (Global Cache sections)."),
    (("gcs log flush sync",), "Cluster",
     "LMS waits for LGWR before shipping blocks: check redo write latency (log file parallel write) on all instances."),
    (("arch wait on sendreq", "lns wait", "data guard", "lgwr-lns"), "Network",
     "Redo transport to standby is slow: check Data Guard network bandwidth/latency and transport mode (ASYNC/SYNC)."),
    (("sql*net more data from dblink", "sql*net message from dblink"), "Network",
     "Time spent waiting for a remote database over a DB link: tune the remote SQL or reduce round-trips/volume."),
    (("sql*net more data to client", "sql*net more data from client"), "Network",
     "Large result sets / arrays over the network: increase array fetch size, SDU, or check client-side network."),
    (("px deq",), "Other", "Parallel query messaging: check DOP and PX skew; reduce parallelism on OLTP."),
    (("cell single block physical read",), "User I/O",
     "Exadata single-block reads: review index access paths of top SQL and flash/PMEM cache hit ratios."),
    (("cell smart table scan", "cell multiblock physical read"), "User I/O",
     "Exadata full scans: check that smart scan offload is effective and statistics are current."),
]


def recommend(rep: dict) -> list[dict]:
    recs = []
    if rep.get("report_type") in ("ash", "ash_rac"):
        recs.append({"severity": "info", "finding": "ASH report detected",
                     "advice": "Percentages are % of sampled activity (not % DB time). Combine with an AWR report "
                               "for wait counts and load profile."})
    for w in rep.get("top_waits", [])[:10]:
        ev = (w.get("event") or "").lower()
        pct = w.get("pct_db_time") or 0
        if ev in ("db cpu", "cpu time", "cpu + wait for cpu"):
            if pct >= 50:
                recs.append({"severity": "medium", "finding": f"{w['event']} is {pct}% of DB time",
                             "advice": "CPU-bound: tune top SQL by CPU / logical reads; verify host CPU is not saturated."})
            continue
        for keys, cls, adv in RULES:
            if any(k in ev for k in keys) and pct >= 5:
                recs.append({"severity": "high" if pct >= 20 else "medium",
                             "finding": f"'{w['event']}' = {pct}% of DB time", "advice": adv})
                break
    for s in rep.get("top_sql", [])[:5]:
        if (s.get("pct_total") or 0) >= 20:
            recs.append({"severity": "high", "finding": f"SQL {s['sql_id']} = {s['pct_total']}% of DB time"
                         + (" (ASH activity)" if "pct_activity" in s else ""),
                         "advice": "Single statement dominates: get its plan (DBMS_XPLAN.DISPLAY_AWR) and tune it first."})
        ex, el = s.get("executions"), s.get("elapsed_s")
        if ex and el is not None and ex > 10000 and el / ex < 0.01:
            recs.append({"severity": "low", "finding": f"SQL {s['sql_id']} executed {int(ex)} times",
                         "advice": "Very high execution count: check for row-by-row processing; consider bulk/array operations."})
    lp = {k.lower(): v for k, v in rep.get("load_profile", {}).items()}
    hp = next((v for k, v in lp.items() if k.startswith("hard parse")), None)
    if hp and hp > 100:
        recs.append({"severity": "medium", "finding": f"Hard parses {hp}/s",
                     "advice": "Excessive hard parsing: use bind variables."})
    lg = next((v for k, v in lp.items() if k.startswith("logon")), None)
    if lg and lg > 5:
        recs.append({"severity": "medium", "finding": f"Logons {lg}/s",
                     "advice": "High connect/disconnect rate: use a connection pool."})
    if rep.get("elapsed_min") and rep.get("db_time_min") and "avg_active_sessions" not in rep:
        rep["avg_active_sessions"] = round(rep["db_time_min"] / rep["elapsed_min"], 2)
    cpus = (rep.get("db") or {}).get("cpus")
    aas = rep.get("avg_active_sessions")
    if cpus and aas and aas > cpus:
        recs.append({"severity": "high", "finding": f"Average active sessions {aas} > {int(cpus)} CPUs",
                     "advice": "The database demands more than the host can serve; sessions queue for CPU or I/O."})
    inst = [i for i in rep.get("instances", []) if i.get("db_time_min")]
    if len(inst) > 1:
        hi, lo = max(inst, key=lambda i: i["db_time_min"]), min(inst, key=lambda i: i["db_time_min"])
        if lo["db_time_min"] and hi["db_time_min"] / lo["db_time_min"] >= 1.8:
            recs.append({"severity": "low", "finding": f"RAC load imbalance: {hi['instance']} has "
                         f"{round(hi['db_time_min'] / lo['db_time_min'], 1)}x the DB time of {lo['instance']}",
                         "advice": "Check service placement and connection load balancing."})
    return recs


def analyze(content: str) -> dict:
    rep = parse_html(content) if re.search(r"<\s*(html|table)", content, re.I) else parse_text(content)
    rep["top_waits"].sort(key=lambda w: w.get("pct_db_time") or 0, reverse=True)
    rep["recommendations"] = recommend(rep)
    from . import advice as A
    ver = A._ver((rep.get("db") or {}).get("release"))
    for w in rep["top_waits"]:
        w["advice"] = A.wait_advice(w.get("event"), w.get("wait_class"), ver)
    for i in rep.get("instances", []):
        for w in i.get("top_waits", []):
            w["advice"] = A.wait_advice(w.get("event"), None, ver)
    for sq in rep["top_sql"]:
        sq["advice"] = A.sql_advice(sq, ver)
    rep["tuning_summary"] = A.tuning_summary(rep, ver)
    return rep
