"""Tuning-advice knowledge base for AWR/ASH wait events and top SQL.

Kept separate from the parsers so DBAs can maintain it. Every advice string is 1-2 sentences.
`ver` is the major version number (10, 11, 12, 19, 23 ...) or None when unknown.
"""
from __future__ import annotations
import re

# (match substrings (lowercase), advice, optional {min_version: extra advice})
WAITS = [
    (("db cpu", "cpu time", "cpu + wait for cpu"),
     "Find the top SQL by CPU / buffer gets and cut logical I/O (better plans, fewer executions); if AAS exceeds the "
     "CPU count, sessions are queueing for CPU.",
     {12: "Check the host for non-Oracle CPU load and consider Resource Manager to protect critical services."}),
    (("log file sync",),
     "Commit less often (batch commits, no COMMIT in loops) and compare with 'log file parallel write': if that is also "
     "slow, move redo to faster storage.",
     {12: "On 12c+ check adaptive log file sync (_use_adaptive_log_file_sync) and that LGWR is not starved of CPU."}),
    (("log file parallel write",),
     "Background redo write latency: put online redo logs on low-latency storage and avoid sharing it with datafile I/O."),
    (("db file sequential read",),
     "Single-block reads: tune the top SQL by physical reads (selective indexes, clustering factor) and check I/O "
     "latency is under ~5 ms; a larger buffer cache helps only if the working set fits."),
    (("db file scattered read",),
     "Multiblock full scans: look for missing indexes or stale statistics on the top SQL by reads, or make the scans "
     "cheaper with partition pruning or compression."),
    (("db file parallel read",),
     "Prefetch/batched index reads: usually driven by nested loops on large row sources; review those plans."),
    (("direct path read temp", "direct path write temp"),
     "Sorts/hash joins spilling to TEMP: raise PGA_AGGREGATE_TARGET or tune the SQL to process fewer rows."),
    (("direct path read",),
     "Serial direct path full scans bypassing the cache: confirm the scans are needed, add indexes or partition "
     "pruning, or use parallel query in batch windows."),
    (("direct path write",),
     "Direct-path loads or temp writes: check the I/O subsystem write throughput and whether NOLOGGING/APPEND is appropriate."),
    (("gc cr block busy", "gc current block busy", "gc buffer busy"),
     "Blocks are contended across RAC instances: route each workload to one instance via services and spread hot "
     "blocks (hash-partitioned/reverse-key indexes, smaller blocks for hot tables)."),
    (("gc cr multi block", "gc cr block 2-way", "gc current block 2-way", "gc cr grant", "gc current grant",
      "gc cr block 3-way", "gc current block 3-way"),
     "Inter-instance block shipping: reduce cross-instance access with service affinity and tune the SQL with most "
     "cluster wait time; check interconnect latency in the Global Cache sections."),
    (("gcs log flush sync",),
     "LMS must wait for LGWR before shipping a block: fix redo write latency on all instances and reduce commit rate."),
    (("library cache: mutex x", "library cache lock", "library cache pin"),
     "Mutex contention on hot cursors/objects: reduce parse and execute frequency of the hottest SQL (session cursor "
     "cache, hold cursors in the app) and avoid DDL/grants during business hours.",
     {11: "Check for very high version counts and consider DBMS_SHARED_POOL.MARKHOT for the hottest cursors."}),
    (("cursor: pin s wait on x",),
     "A session waits while another hard-parses the same cursor: eliminate hard parses (bind variables) and look for "
     "high version counts in 'SQL ordered by Version Count'."),
    (("cursor: pin s", "cursor: mutex"),
     "Cursor mutex contention from very frequent executions of the same SQL: cut executions (caching, batching)."),
    (("latch: shared pool", "latch: row cache objects", "row cache lock"),
     "Shared pool / dictionary contention: use bind variables, size the shared pool to avoid ORA-4031 flushes and "
     "cache sequences (CACHE 1000+)."),
    (("enq: tx - row lock contention",),
     "Sessions block on the same rows: find the blockers in ASH (blocking_session) and shorten transactions or "
     "change the locking order in the application."),
    (("enq: tx - index contention",),
     "Right-hand index growth on sequence/timestamp keys: use hash-partitioned or reverse-key indexes and a large sequence CACHE."),
    (("enq: tx - allocate itl",),
     "Not enough ITL slots in hot blocks: rebuild the hot table/index with higher INITRANS."),
    (("enq: hw",),
     "High-water-mark contention during concurrent inserts: use ASSM tablespaces, larger uniform extents or "
     "preallocate space (ALTER TABLE ... ALLOCATE EXTENT)."),
    (("enq: tm",),
     "TM lock contention is almost always an unindexed foreign key: index FK columns of child tables used in parent DML."),
    (("enq: sq", "enq: sv"),
     "Sequence contention: increase the sequence CACHE (and avoid ORDER on RAC).",),
    (("buffer busy waits", "read by other session"),
     "Many sessions want the same block: identify the segment in 'Segments by Buffer Busy Waits' and spread the "
     "hot blocks (partitioning, ASSM, reverse-key index)."),
    (("free buffer waits",),
     "DBWR cannot clean buffers fast enough: check datafile write latency, enable async I/O and add DB writers."),
    (("log buffer space",),
     "Redo generation outpaces LGWR: enlarge LOG_BUFFER and speed up redo log storage."),
    (("log file switch",),
     "Log switches are waiting on checkpoint/archiving: add more and larger redo log groups (aim for ~3-4 switches/hour)."),
    (("arch wait on sendreq", "lns wait", "lgwr-lns", "lgwr wait on sendreq", "data guard", "redo transport"),
     "Redo shipping to the standby is slow: check network bandwidth/latency to the standby and the transport mode; "
     "ASYNC transport removes it from the commit path.",
     {11: "On 11g+ consider redo transport compression (COMPRESSION=ENABLE, Advanced Compression licence) and a larger SDU."}),
    (("sql*net more data from dblink", "sql*net message from dblink"),
     "Time waiting on a remote database: tune the remote SQL (DRIVING_SITE hint) or reduce data moved over the DB link."),
    (("sql*net more data to client", "sql*net more data from client"),
     "Large result sets or arrays over the network: raise the client fetch/array size and SDU, or return less data."),
    (("px deq", "px idle", "px qref"),
     "Parallel query messaging/skew: lower the DOP for OLTP, check PX data skew, and avoid parallelism on small tables.",
     {12: "On 12c+ use parallel statement queuing (PARALLEL_DEGREE_POLICY=AUTO) to stop PX oversubscription."}),
    (("resmgr:cpu quantum",),
     "Resource Manager is throttling sessions: review the active resource plan (or CPU_COUNT under instance caging) "
     "versus the real CPU demand."),
    (("cell single block physical read",),
     "Exadata single-block reads: tune index access of the top SQL by reads; check flash/PMEM cache hit ratios.",
     {19: "On 19c with PMEM/XRMEM cache these should be sub-100us; higher averages point to cache misses."}),
    (("cell multiblock physical read", "cell list of blocks"),
     "Exadata multiblock reads that did not use smart scan: check why offload is not used (direct reads, row-by-row access)."),
    (("cell smart table scan", "cell smart index scan"),
     "Smart scans dominate: check offload efficiency and storage indexes, and whether the SQL needs a full scan at all."),
    (("rdbms ipc reply",),
     "Foreground waiting on a background process (often during checkpoints/truncates): check DBWR/CKPT load and "
     "frequent TRUNCATE/DROP of objects."),
    (("dfs lock handle",),
     "RAC global lock waits (often sequences without CACHE or ORDER, or object reuse): check the lock type in ASH p1."),
    (("control file", "db file async i/o submit"),
     "Background I/O waits: verify storage latency and async I/O configuration."),
]
GENERIC_WAIT = ("Look up the event's wait class, find the SQL and sessions waiting on it in ASH "
                "(DBA_HIST_ACTIVE_SESS_HISTORY) and address the dominant contributor.")
IDLE_CLASSES = {"idle"}


def _ver(release):
    m = re.match(r"(\d+)", release or "")
    return int(m.group(1)) if m else None


def wait_advice(event: str, wait_class: str | None = None, ver: int | None = None) -> str:
    ev = (event or "").lower()
    for row in WAITS:
        keys, adv = row[0], row[1]
        if any(k in ev for k in keys):
            extra = row[2] if len(row) > 2 else {}
            for v in sorted(extra, reverse=True):
                if ver is None or ver >= v:
                    return adv + " " + extra[v]
            return adv
    if (wait_class or "").lower() in IDLE_CLASSES:
        return "Idle event; ignore it for tuning."
    return GENERIC_WAIT


def sql_advice(s: dict, ver: int | None = None) -> str:
    el, ex = s.get("elapsed_s"), s.get("executions")
    pct, cpu, io = s.get("pct_total") or 0, s.get("cpu_pct"), s.get("io_pct")
    plan = "DBMS_XPLAN.DISPLAY_AWR('" + str(s.get("sql_id")) + "')"
    tips = []
    if "pct_activity" in s:
        ev = (s.get("top_event") or "").lower()
        tips.append(f"Top ASH statement (mostly '{s.get('top_event')}'): " + wait_advice(ev, None, ver).split(":")[-1].strip()
                    if ev else "Top ASH statement: get its plan with " + plan + ".")
        return " ".join(tips)
    if ex == 0 and el:
        tips.append("Still running at snapshot end (long-running batch/report): check its plan with "
                    f"{plan} and whether it can be parallelised or restricted with partition pruning.")
    elif ex and el is not None and el / ex >= 10:
        tips.append(f"Slow per execution ({el / ex:,.1f} s/exec): check the plan with {plan} for full scans, bad "
                    "join order or stale statistics.")
    elif ex and ex >= 100000 and el is not None:
        tips.append(f"Executed {int(ex):,} times at {1000 * el / ex:,.1f} ms each: " +
                    ("the cost is the call count, so cache the result, batch it (array/FORALL) or move it out of the loop."
                     if el / ex < 0.01 else
                     "shave per-execution cost (fewer buffer gets per row via a better index) and question whether "
                     "every call is needed."))
    if cpu is not None and cpu >= 80:
        tips.append("CPU-bound: reduce buffer gets (more selective index, avoid repeated nested-loop probes).")
    elif io is not None and io >= 50:
        tips.append("I/O-bound: reduce physical reads (indexing, partition pruning) or check storage latency.")
    if pct >= 20:
        tips.insert(0, f"Uses {pct}% of DB time; tune this first.")
    if ver and ver >= 11 and pct >= 20:
        tips.append("Once a good plan is found, fix it with a SQL plan baseline.")
    elif ver and ver < 11 and pct >= 20:
        tips.append("On 10g a SQL Profile from the SQL Tuning Advisor (Tuning Pack) is the way to fix the plan.")
    return " ".join(tips) or f"Moderate contributor; review its plan with {plan} if it grows."


def tuning_summary(rep: dict, ver: int | None = None, n: int = 3) -> list[str]:
    """Top-n prioritised actions: biggest non-idle wait, biggest SQL, then structural issues."""
    acts = []
    waits = [w for w in rep.get("top_waits", []) if (w.get("pct_db_time") or 0) >= 5
             and (w.get("wait_class") or "").lower() not in IDLE_CLASSES]
    sqls = [s for s in rep.get("top_sql", []) if (s.get("pct_total") or 0) >= 10]
    for w in waits[:3]:
        acts.append((w["pct_db_time"], f"{w['event']} ({w['pct_db_time']}%): {w.get('advice') or wait_advice(w['event'], None, ver)}"))
    if not sqls and rep.get("top_sql"):
        sqls = rep["top_sql"][:1]
    for s in sqls[:2]:
        acts.append((s.get("pct_total") or 0, f"SQL {s['sql_id']} ({s.get('pct_total')}%): {s.get('advice') or sql_advice(s, ver)}"))
    for r in rep.get("recommendations", []):
        if any(k in r["finding"] for k in ("load imbalance", "CPUs", "Logons", "Hard parses")):
            acts.append((15 if r["severity"] == "high" else 6, f"{r['finding']}: {r['advice']}"))
    acts.sort(key=lambda a: -a[0])
    out, seen = [], set()
    for _, a in acts:
        if a not in seen:
            seen.add(a); out.append(a)
    return [f"{i + 1}. {a}" for i, a in enumerate(out[:n])]
