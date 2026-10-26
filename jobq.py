#!/usr/bin/env python3
"""jobq: a memory-aware job queue for a shared Linux machine used by several people and AI agents.

State lives next to this file (or in $JOBQ_DIR); AGENTS.md is the guide for the agents that submit jobs.

Every job declares the threads it uses, the peak memory it may need and (optionally) the GPU memory. The daemon starts a
pending job only when
  * the threads of the running jobs plus the job's threads stay within the budget (default 1.5 x cores),
  * MemAvailable minus the not-yet-used part of the running jobs' memory leaves the job's memory plus a margin (3 GB),
  * (GPU jobs) the GPU has that much free memory,
  * no high-priority job is waiting (high-priority jobs go first; normal jobs backfill only when none is blocked).
Jobs run under `nice -n 19 ionice -c3` in jobq.slice (CPU weight 1: other users' work always wins). Each job gets its
own cgroup with a 50 GB RAM cap (beyond it, swap). Admission overbooks: a running job reserves its real use plus half
of the rest of its declaration, and a new job needs half of its declaration (+ margin) free. Memory guard (every 5 s):
free RAM < 5 GB -> the fastest-growing job above 20 GB (else the newest CPU job) is frozen and pushed to swap; it
resumes when RAM frees up. Other jobs start around a frozen job; after 2 h frozen, new starts stop until it resumes.
GPU jobs are never frozen.
Optionally (install.sh --exclusive-swap) jobq.slice is the only slice allowed to swap.

Watchdog: leftover processes of a finished job are killed; a running job that holds > 1 GB RAM and used no CPU for
--stall-min minutes (default 60, 0 = never) is killed; idle processes outside the queue that hold > 2 GB are reported.

  jobq add --name NAME --threads T --mem GB [--gpu MB] [--high] [--cwd DIR] -- command ...
  jobq status [--json] [--all]       jobq wait NAME       jobq log NAME       jobq cancel NAME
  jobq daemon                        (run by systemd: jobq.service)
  touch $JOBQ_DIR/PAUSE      (no new job starts until removed; running jobs continue)
  touch $JOBQ_DIR/RELOAD     (the daemon re-executes itself with the current code; jobs keep running)
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

Q = Path(os.environ.get("JOBQ_DIR", Path(__file__).resolve().parent))
# job project tag = first folder below this root in the job's working dir (default: the folder holding the queue dir)
PROJECTS = Path(os.environ.get("JOBQ_PROJECTS_ROOT", Q.parent))
PENDING, RUNNING, DONE, LOGS = Q / "pending", Q / "running", Q / "done", Q / "logs"
BIG_GB = 50.0          # RAM cap of a single job (whatever it declared); beyond it the job pages to swap
THREAD_BUDGET = int(1.5 * os.cpu_count())   # declared threads of running jobs (jobs often declare more than they use)
MARGIN_GB = 3.0        # RAM always left free for the OS and the other users
STALL_RAM_GB = 1.0     # a stalled job is killed only if it holds at least this much RAM
OVERBOOK = 0.5        # a running job reserves its real RAM use plus this share of the rest of its declaration
FREEZE_BELOW_GB = 5.0  # free RAM below this: freeze the newest CPU job and push it to swap
RESUME_ABOVE_GB = 10.0 # a frozen job resumes when free RAM minus its swapped-out memory stays above this
DRAIN_AFTER_H = 2.0    # other jobs keep starting around a frozen job; after this long frozen, new starts stop until it resumes
GROWER_GB = 20.0       # a job holding more than this that grew fastest in the last 5 min is frozen first
STARVE_MIN = 30       # a job waiting this long for RAM/threads stops later jobs from starting ahead of it
HOG_RAM_GB, HOG_IDLE_MIN = 2.0, 30   # report idle processes outside the queue above this RAM
SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL", re.I)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def mem_available_gb() -> float:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def gpu_free_mb() -> float:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
        return float(out.split()[0])
    except Exception:
        return 0.0


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"   # a zombie counts as finished
    except (OSError, ValueError, TypeError):
        return False


def system_usage() -> dict:
    """Whole machine, all users: busy cores (0.5 s sample), RAM, swap and GPU memory in use."""
    def ticks():
        f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return sum(f), f[3] + f[4]          # total, idle + iowait
    t0, i0 = ticks(); time.sleep(0.5); t1, i1 = ticks()
    m = {l.split(":")[0]: int(l.split()[1]) / 1024 ** 2 for l in open("/proc/meminfo")}
    try:
        g = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=20).stdout.split(",")
        gpu = (float(g[0]) / 1024, float(g[1]) / 1024)
    except Exception:
        gpu = (0.0, 0.0)
    return {"cores_busy": round(os.cpu_count() * (1 - (i1 - i0) / max(1, t1 - t0)), 1), "cores": os.cpu_count(),
            "ram_used_gb": round(m["MemTotal"] - m["MemAvailable"], 1), "ram_total_gb": round(m["MemTotal"]),
            "swap_used_gb": round(m["SwapTotal"] - m["SwapFree"], 1), "swap_total_gb": round(m["SwapTotal"]),
            "gpu_used_gb": round(gpu[0], 1), "gpu_total_gb": round(gpu[1])}


def tree_pids(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-e", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    children = {}
    for line in out.split("\n"):
        f = line.split()
        if len(f) == 2:
            children.setdefault(int(f[1]), []).append(int(f[0]))
    res, stack = [], [pid]
    while stack:
        p = stack.pop()
        res.append(p)
        stack.extend(children.get(p, []))
    return res


def proc_rss_cpu(pid: int) -> tuple[float, float]:
    """(resident GB, CPU seconds) of one process."""
    try:
        f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        return int(f[21]) * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3, (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return 0.0, 0.0


def job_usage(j: dict) -> tuple[float, float, float]:
    """(RAM GB without page cache, swap GB, CPU seconds) of a running job: from its cgroup, else from its process tree."""
    cg = j.get("cgroup")
    if cg and Path(cg).is_dir():
        try:
            st = dict(l.split() for l in open(f"{cg}/memory.stat"))
            cpu = dict(l.split() for l in open(f"{cg}/cpu.stat"))
            swap = int(open(f"{cg}/memory.swap.current").read())
            return int(st["anon"]) / 1024 ** 3, swap / 1024 ** 3, int(cpu["usage_usec"]) / 1e6
        except (OSError, KeyError, ValueError):
            pass
    ram = cpu = 0.0
    for p in tree_pids(j["pid"]) if j.get("pid") else []:
        r, c = proc_rss_cpu(p)
        ram, cpu = ram + r, cpu + c
    return ram, 0.0, cpu


def ram_limit(j: dict) -> float:
    return j["ram"] if j.get("ram") else BIG_GB


def charge(j: dict) -> float:
    """RAM the job can actually occupy (a job declaring more than its cap holds the cap and swaps the rest)."""
    return min(j["mem"], ram_limit(j))


def load(f: Path):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def save(f: Path, j: dict):
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(j, indent=1))
    tmp.replace(f)


def jobs(d: Path) -> list[tuple[Path, dict]]:
    res = [(f, load(f)) for f in sorted(d.glob("*.json"))] if d.exists() else []
    return [(f, j) for f, j in res if j]


def project_of(cwd: str) -> str:
    p = Path(cwd).resolve()
    parts = p.relative_to(PROJECTS).parts if p.is_relative_to(PROJECTS) else ()
    return parts[0] if parts else p.name


# ---- per-job cgroups (the daemon runs in jobq.service, a delegated unit in jobq.slice): each job gets its own child
# cgroup with a RAM cap (memory.max) and swap allowed
CG_ROOT = None


def cg_setup(log):
    """Move the daemon into <unit>/daemon and enable the memory controller for job cgroups."""
    global CG_ROOT
    try:
        rel = open("/proc/self/cgroup").read().strip().split("::", 1)[1]
        base = Path("/sys/fs/cgroup" + rel)
        if base.name == "daemon":
            base = base.parent
        if not os.access(base / "cgroup.subtree_control", os.W_OK):
            log.write(f"{now()} no per-job cgroups: {base} not delegated (RAM caps by ulimit, no swap overflow)\n")
            return
        (base / "daemon").mkdir(exist_ok=True)
        # cgroup v2 "no internal processes": every process of the unit itself moves to daemon/
        for pid in (base / "cgroup.procs").read_text().split():
            try:
                (base / "daemon" / "cgroup.procs").write_text(pid)
            except OSError:
                pass
        (base / "cgroup.subtree_control").write_text("+memory +pids")
        CG_ROOT = base
        log.write(f"{now()} per-job cgroups under {base}\n")
    except Exception as e:
        log.write(f"{now()} no per-job cgroups: {e}\n")


def cg_job(stem: str, ram_gb: float):
    if CG_ROOT is None:
        return None
    cg = CG_ROOT / f"job-{stem}"
    try:
        cg.mkdir(exist_ok=True)
        (cg / "memory.max").write_text(str(int(ram_gb * 1024 ** 3)))
        (cg / "memory.swap.max").write_text("max")
        return cg
    except Exception:
        return None


def cg_kill(cg) -> int:
    """Kill every process left in a job cgroup; returns how many there were."""
    try:
        n = len(Path(cg, "cgroup.procs").read_text().split())
        if n:
            Path(cg, "cgroup.kill").write_text("1")
        return n
    except OSError:
        return 0


def kill_job(j: dict):
    if j.get("cgroup") and cg_kill(j["cgroup"]):
        return
    try:
        os.killpg(j["pid"], signal.SIGTERM)
    except (OSError, KeyError, TypeError):
        pass


# ---- commands

def cmd_add(a):
    if not a.command:
        sys.exit("no command")
    if not re.fullmatch(r"[\w.+-]+", a.name):
        sys.exit("--name: letters, digits, _ . + - only")
    cwd = str(Path(a.cwd).resolve())
    env = {k: v for k, v in os.environ.items() if not SECRET.search(k)}
    job = {"name": a.name, "project": a.project or project_of(cwd), "threads": a.threads, "mem": a.mem, "gpu": a.gpu,
           "high": a.high, "ram": a.ram, "stall_min": a.stall_min, "cwd": cwd, "cmd": a.command, "env": env,
           "added": now()}
    for d in (PENDING, RUNNING):
        for _, j in jobs(d):
            if j["name"] == a.name and j["project"] == job["project"]:
                print(f"warning: {job['project']}/{a.name} is already {d.name}", file=sys.stderr)
    PENDING.mkdir(parents=True, exist_ok=True)
    f = PENDING / f"{time.time_ns()}_{a.name}.json"
    save(f, job)
    lim = ram_limit(job)
    note = f", RAM cap {lim:.0f} GB (rest to swap)" if job["mem"] > lim else ""
    print(f"queued {job['project']}/{a.name}{' [HIGH]' if a.high else ''} ({f.stem}){note}")


def state() -> dict:
    return load(Q / "state.json") or {}


def status_dict(n_done: int) -> dict:
    st = state()
    running = []
    for f, j in jobs(RUNNING):
        ram, swap, _ = job_usage(j)
        running.append({k: j.get(k) for k in ("name", "project", "threads", "mem", "gpu", "high", "pid", "started",
                                              "log", "stalled_since", "frozen")} | {"id": f.stem, "ram_gb": round(ram, 2),
                                                                         "swap_gb": round(swap, 2), "ram_cap_gb": ram_limit(j)})
    pending = [{k: j.get(k) for k in ("name", "project", "threads", "mem", "gpu", "high", "added")}
               | {"id": f.stem, "waiting_for": st.get("waiting", {}).get(f.stem, "next daemon pass")}
               for f, j in sorted(jobs(PENDING), key=lambda fj: (not fj[1].get("high"), fj[0].name))]
    done = [{k: j.get(k) for k in ("name", "project", "rc", "started", "ended", "log", "mem", "peak_ram_gb",
                                   "peak_swap_gb", "killed")} | {"id": f.stem} for f, j in sorted(jobs(DONE), key=lambda fj: fj[1].get("ended") or "")[-n_done:][::-1]]
    up = alive(st.get("pid")) and time.time() - st.get("heartbeat", 0) < 120
    return {"daemon": "up" if up else "DOWN", "paused": (Q / "PAUSE").exists(), "cgroups": st.get("cgroups"),
            "threads_budget": st.get("budget"), "threads_used": sum(r["threads"] for r in running),
            "mem_available_gb": round(mem_available_gb(), 1), "gpu_free_mb": gpu_free_mb(), "system": system_usage(),
            "running": running, "pending": pending, "done": done, "idle_hogs": st.get("hogs", [])}


def since(t: str | None) -> str:
    """Duration from a '%Y-%m-%d %H:%M:%S' time until now, as 3d 04:05 / 04:05."""
    if not t:
        return ""
    m = int(time.time() - time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))) // 60
    return (f"{m // 1440}d " if m >= 1440 else "") + f"{m % 1440 // 60:02d}:{m % 60:02d}"


def span(a: str | None, b: str | None) -> str:
    if not a or not b:
        return ""
    m = int(time.mktime(time.strptime(b, "%Y-%m-%d %H:%M:%S")) - time.mktime(time.strptime(a, "%Y-%m-%d %H:%M:%S"))) // 60
    return (f"{m // 1440}d " if m >= 1440 else "") + f"{m % 1440 // 60:02d}:{m % 60:02d}"


def cg_text(j: dict) -> str:
    """Threads as C, GPU memory as G: '4C', '1C + G 3.0 GB'."""
    return f"{j['threads']}C" + (f" + G {j['gpu'] / 1024:.1f} GB" if j.get("gpu") else "")


def num(x) -> str:
    return "-" if x is None else f"{x:.1f}"


def gb(x) -> str:
    return "-" if x is None else f"{x:.1f} GB"


def table(head: list[str], rows: list[list], right: str) -> list[str]:
    """Markdown table with padded columns, so it is aligned in the raw file too. right: 'r'/'l' per column."""
    rows = [[str(c) for c in r] for r in rows]
    w = [max(len(x) for x in col) for col in zip(head, *rows)]
    cell = lambda x, i: x.rjust(w[i]) if right[i] == "r" else x.ljust(w[i])
    line = lambda r: "| " + " | ".join(cell(x, i) for i, x in enumerate(r)) + " |"
    rule = "|" + "|".join("-" * (w[i] + 1) + ":" if right[i] == "r" else "-" * (w[i] + 2) for i in range(len(w))) + "|"
    return [line(head), rule] + [line(r) for r in rows]


def render_md(s: dict) -> str:
    """The queue as a Markdown page (QUEUE.md, rewritten by the daemon on every pass)."""
    L = ["# Job queue", "",
         f"Updated {now()} (every 30 s)  ",
         f"Daemon {s['daemon']}{', PAUSED (no new starts)' if s['paused'] else ''} | threads {s['threads_used']}/"
         f"{s['threads_budget']} | free RAM {s['mem_available_gb']} GB | free GPU {s['gpu_free_mb'] / 1024:.1f} GB", "",
         f"## Running ({len(s['running'])})", ""]
    u = s["system"]
    rows = [["SYSTEM", "", f"{u['cores_busy']:.0f}/{u['cores']}C + G {u['gpu_used_gb']:.1f}/{u['gpu_total_gb']}",
             f"{sum(r['mem'] for r in s['running']):.1f}", f"{u['ram_used_gb']:.1f}/{u['ram_total_gb']}",
             f"{u['swap_used_gb']:.1f}/{u['swap_total_gb']}"]]
    for r in sorted(s["running"], key=lambda r: r["started"] or ""):
        flag = (" HIGH" if r.get("high") else "") + (" STALLED" if r.get("stalled_since") else "") + \
               (" FROZEN" if r.get("frozen") else "")
        rows.append([f"{r['project']}/{r['name']}{flag}", since(r["started"]), cg_text(r),
                     f"{r['mem']:.1f}", f"{r['ram_gb']:.1f}", f"{r['swap_gb']:.1f}"])
    L += table(["Job", "For", "CPU/GPU", "Decl GB", "RAM GB", "Swap GB"], rows, "lllrrr")
    L += ["", f"## Next up ({len(s['pending'])}, in start order)", ""]
    if s["pending"]:
        rows = [[i, f"{p['project']}/{p['name']}", "HIGH" if p["high"] else "", p["added"][5:16], cg_text(p),
                 f"{p['mem']:.1f}", p["waiting_for"]] for i, p in enumerate(s["pending"], 1)]
        L += table(["#", "Job", "Prio", "Queued", "CPU/GPU", "Decl GB", "Waiting for"], rows, "rllllrl")
        L += ["", "Smaller jobs further down may start first when they fit (backfill), but not ahead of a HIGH job or a job "
              "that has waited more than 30 min."]
    else:
        L.append("Queue empty.")
    L += ["", "## Recently finished", ""]
    if s["done"]:
        rows = []
        for d in s["done"]:
            res = "ok" if d["rc"] == 0 else f"FAIL rc {d['rc']}" + (f" ({d['killed']})" if d.get("killed") else "")
            rows.append([f"{d['project']}/{d['name']}", res, (d["ended"] or "")[5:16], span(d["started"], d["ended"]),
                         num(d.get("mem")), num(d.get("peak_ram_gb")), num(d.get("peak_swap_gb"))])
        L += table(["Job", "Result", "Ended", "Took", "Decl GB", "Peak RAM GB", "Peak swap GB"], rows, "llllrrr")
    else:
        L.append("None yet.")
    week = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7 * 86400))
    per = {}
    for _, j in jobs(DONE):
        if (j.get("ended") or "") >= week and j.get("peak_ram_gb") is not None and j.get("mem"):
            n, dsum, psum, mx = per.get(j["project"], (0, 0.0, 0.0, 0.0))
            per[j["project"]] = (n + 1, dsum + j["mem"], psum + j["peak_ram_gb"], max(mx, j["peak_ram_gb"] / j["mem"]))
    if per:
        L += ["", "## Declared vs peak RAM per project (finished jobs, last 7 days)", ""]
        L += table(["Project", "Jobs", "Decl GB", "Peak GB", "Decl/peak", "Worst peak/decl"],
                   [[p, n, f"{d:.0f}", f"{pk:.0f}", f"{d / max(pk, 0.1):.1f}x", f"{mx:.0%}"]
                    for p, (n, d, pk, mx) in sorted(per.items())], "lrrrrr")
        L += ["", "Peak includes the job's file cache, so it slightly overstates real use."]
    if s["idle_hogs"]:
        L += ["", "## Idle processes outside the queue holding > 2 GB (reported, not killed)", ""]
        L += table(["PID", "User", "RAM GB", "Idle min", "Command"],
                   [[h["pid"], h["user"], h["rss_gb"], h["idle_min"], h["cmd"][:60].replace("|", "/")] for h in s["idle_hogs"]],
                   "rlrrl")
    return "\n".join(L) + "\n"


def cmd_status(a):
    s = status_dict(10 ** 6 if a.all else 10)
    if a.md:
        print(render_md(s), end="")
        return
    if a.json:
        print(json.dumps(s, indent=1))
        return
    print(f"daemon {s['daemon']}{' PAUSED' if s['paused'] else ''}, threads {s['threads_used']}/{s['threads_budget']}, "
          f"MemAvailable {s['mem_available_gb']} GB, GPU free {s['gpu_free_mb']:.0f} MB, "
          f"per-job cgroups {'yes' if s['cgroups'] else 'NO'}")
    for r in s["running"]:
        stall = (f"  STALLED since {r['stalled_since']}" if r.get("stalled_since") else "") + \
                (f"  FROZEN since {r['frozen']}" if r.get("frozen") else "")
        print(f"RUNNING  {r['project'] + '/' + r['name']:45s} thr {r['threads']:2d} mem {r['mem']:5.1f} GB  "
              f"now {r['ram_gb']:5.1f} GB + swap {r['swap_gb']:5.1f} GB  since {r['started']}{stall}")
    for p in s["pending"]:
        print(f"PENDING{'*' if p['high'] else ' '} {p['project'] + '/' + p['name']:45s} thr {p['threads']:2d} "
              f"mem {p['mem']:5.1f} GB  waits for {p['waiting_for']}")
    for d in s["done"]:
        extra = f"  ({d['killed']})" if d.get("killed") else ""
        print(f"DONE     {d['project'] + '/' + d['name']:45s} rc {d['rc']}  {d['started']} -> {d['ended']}{extra}")
    for h in s["idle_hogs"]:
        print(f"IDLE-HOG pid {h['pid']} {h['user']} {h['rss_gb']} GB idle {h['idle_min']} min: {h['cmd'][:80]}")


def find(name: str, project: str | None, dirs) -> list[tuple[Path, dict]]:
    return [(f, j) for d in dirs for f, j in jobs(d)
            if (j["name"] == name or f.stem == name) and (project is None or j["project"] == project)]


def cmd_cancel(a):
    for f, j in find(a.name, a.project, (PENDING, RUNNING)):
        if f.parent == PENDING:
            f.unlink(missing_ok=True)
        else:
            j["killed"] = "cancelled"
            save(f, j)
            kill_job(j)
        print("cancelled", j["project"] + "/" + j["name"], f.stem)


def cmd_wait(a):
    while find(a.name, a.project, (PENDING, RUNNING)):
        time.sleep(a.poll)
    done = find(a.name, a.project, (DONE,))
    if not done:
        sys.exit(f"no job {a.name}")
    j = done[-1][1]
    print(f"{j['project']}/{j['name']} rc {j['rc']} log {j.get('log')}")
    sys.exit(0 if j["rc"] == 0 else 1)


def cmd_log(a):
    hits = find(a.name, a.project, (RUNNING, DONE))
    if not hits:
        sys.exit(f"no started job {a.name}")
    log = hits[-1][1]["log"]
    print(f"==> {log}")
    sys.stdout.flush()
    subprocess.run(["tail", "-n", str(a.lines), log])


# ---- daemon

def finish(f: Path, j: dict, rc, log, reason=None):
    if j.get("cgroup"):
        cg = Path(j["cgroup"])
        try:
            j["peak_ram_gb"] = round(int((cg / "memory.peak").read_text()) / 1024 ** 3, 2)
            j["peak_swap_gb"] = round(int((cg / "memory.swap.peak").read_text()) / 1024 ** 3, 2)
        except (OSError, ValueError):
            pass
        n = cg_kill(cg)
        if n:
            log.write(f"{now()} killed {n} leftover processes of {j['name']}\n")
    if reason:
        j["killed"] = reason
    j["rc"], j["ended"] = rc, now()
    j.pop("env", None)
    save(DONE / f.name, j)
    f.unlink(missing_ok=True)
    log.write(f"{now()} end {j['project']}/{j['name']} rc {rc}{' (' + j['killed'] + ')' if j.get('killed') else ''}\n")


def sweep_cgroups(running_cgs: set[str]):
    """Kill and remove job cgroups that no running job owns (leftovers of finished or lost jobs)."""
    if CG_ROOT is None:
        return
    for cg in CG_ROOT.glob("job-*"):
        if str(cg) not in running_cgs:
            cg_kill(cg)
            try:
                cg.rmdir()
            except OSError:
                pass


def scan_hogs(seen: dict) -> list[dict]:
    """Processes outside the queue that hold > HOG_RAM_GB and used no CPU for HOG_IDLE_MIN minutes (report only)."""
    t, hogs, live = time.time(), [], set()
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        rss, cpu = proc_rss_cpu(pid)
        if rss < HOG_RAM_GB:
            continue
        try:
            if "jobq" in open(f"/proc/{pid}/cgroup").read():
                continue
            uid = os.stat(f"/proc/{pid}").st_uid
            cmd = open(f"/proc/{pid}/cmdline").read().replace("\0", " ").strip()
        except OSError:
            continue
        live.add(pid)
        last_cpu, since = seen.get(pid, (None, t))
        if last_cpu is None or cpu - last_cpu > 1.0:
            since = t
        seen[pid] = (cpu, since)
        idle = (t - since) / 60
        if idle >= HOG_IDLE_MIN:
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
            hogs.append({"pid": pid, "user": user, "rss_gb": round(rss, 1), "idle_min": int(idle), "cmd": cmd})
    for pid in set(seen) - live:
        del seen[pid]
    return hogs


def mem_guard(g: dict, log):
    """Free RAM below FREEZE_BELOW_GB: freeze a CPU job and push its memory to swap: the fastest-growing job holding more
    than GROWER_GB, else the newest (HIGH jobs last, GPU jobs never). Free RAM above RESUME_ABOVE_GB: thaw the oldest
    frozen job that fits."""
    t, free = time.time(), mem_available_gb()
    run = jobs(RUNNING)
    hist = g.setdefault("hist", {})       # job id -> [(time, RAM GB)] over the last 5 min
    for f, j in run:
        h = hist.setdefault(f.stem, [])
        h.append((t, job_usage(j)[0]))
        while h and t - h[0][0] > 300:
            h.pop(0)
    for k in set(hist) - {f.stem for f, _ in run}:
        del hist[k]
    if free < FREEZE_BELOW_GB:
        if t - g.get("last_freeze", 0) < 20 and free > 2.0:     # give the last freeze time to reach swap
            return
        cands = [(f, j, job_usage(j)[0]) for f, j in run
                 if j.get("cgroup") and not j.get("frozen") and j["gpu"] <= 0]
        cands = sorted([c for c in cands if c[2] >= 0.5], key=lambda c: c[1].get("started") or "", reverse=True)
        cands.sort(key=lambda c: bool(c[1].get("high")))
        growth = {c[0].stem: c[2] - hist[c[0].stem][0][1] for c in cands if hist.get(c[0].stem)}
        big = [c for c in cands if c[2] > GROWER_GB and growth.get(c[0].stem, 0) > 0 and not c[1].get("high")]
        if big:
            cands = sorted(big, key=lambda c: growth[c[0].stem], reverse=True)
        if not cands:
            if t - g.get("last_warn", 0) > 600:
                log.write(f"{now()} free RAM {free:.1f} GB but no job can be frozen\n"); g["last_warn"] = t
            return
        f, j, ram = cands[0]
        g["last_freeze"] = t
        try:
            Path(j["cgroup"], "cgroup.freeze").write_text("1")
        except OSError as e:
            log.write(f"{now()} cannot freeze {j['name']}: {e}\n"); return
        j["frozen"] = now(); save(f, j)
        # push the frozen job's memory to swap in the background (the write blocks until it is reclaimed)
        subprocess.Popen(["sh", "-c", f"echo {int(ram * 1024 ** 3)} > {j['cgroup']}/memory.reclaim"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.write(f"{now()} free RAM {free:.1f} GB: froze {j['project']}/{j['name']} ({ram:.1f} GB to swap)\n")
    elif free > RESUME_ABOVE_GB and t - g.get("last_thaw", 0) > 60:
        fr = sorted([(f, j) for f, j in run if j.get("frozen")], key=lambda fj: fj[1].get("started") or "")
        if not fr:
            return
        others_active = any(not x.get("frozen") for _, x in run)
        # oldest first, but a frozen job that cannot come back yet must not keep a smaller one frozen
        for f, j in fr:
            ram, swap, _ = job_usage(j)
            back = min(swap, max(0.0, ram_limit(j) - ram))     # what it pages back into RAM (at most up to its cap)
            if free - back >= RESUME_ABOVE_GB or not others_active:
                break
        else:
            return
        try:
            Path(j["cgroup"], "cgroup.freeze").write_text("0")
        except OSError as e:
            log.write(f"{now()} cannot thaw {j['name']}: {e}\n")
        j["frozen_min"] = j.get("frozen_min", 0) + int((t - time.mktime(time.strptime(j["frozen"], "%Y-%m-%d %H:%M:%S"))) / 60)
        j.pop("frozen"); save(f, j); g["last_thaw"] = t
        log.write(f"{now()} free RAM {free:.1f} GB: resumed {j['project']}/{j['name']}\n")


def cmd_daemon(a):
    for d in (PENDING, RUNNING, DONE, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    log = open(Q / "daemon.log", "a", buffering=1)
    old = state().get("pid")
    if old and old != os.getpid() and alive(old) and "jobq" in open(f"/proc/{old}/cmdline").read():
        sys.exit(f"daemon already running (pid {old})")
    cg_setup(log)
    budget = a.budget or THREAD_BUDGET
    log.write(f"{now()} daemon start pid {os.getpid()} budget {budget} threads, margin {a.margin} GB\n")
    procs: dict[str, subprocess.Popen] = {}
    progress: dict[str, tuple[float, float]] = {}   # job id -> (CPU seconds, time CPU last advanced)
    seen_hogs: dict[int, tuple] = {}
    guard: dict = {}
    for _, j in jobs(RUNNING):      # running jobs get the current RAM cap
        if j.get("cgroup") and Path(j["cgroup"]).is_dir():
            try:
                Path(j["cgroup"], "memory.max").write_text(str(int(ram_limit(j) * 1024 ** 3)))
            except OSError:
                pass
    hogs: list[dict] = []
    last_gpu_start, tick = 0.0, 0
    while True:
        # reap finished jobs and kill stalled ones
        for f, j in jobs(RUNNING):
            p = procs.get(f.stem)
            rc = p.poll() if p is not None else (None if alive(j["pid"]) else "lost")
            if rc is not None:
                try:
                    rc = int((LOGS / f"{f.stem}.rc").read_text())
                except (OSError, ValueError):
                    pass
                finish(f, j, rc, log)
                procs.pop(f.stem, None); progress.pop(f.stem, None)
                continue
            ram, _, cpu = job_usage(j)
            if j.get("frozen"):     # frozen by the memory guard: no CPU on purpose
                progress[f.stem] = (cpu, time.time())
                continue
            last_cpu, since = progress.get(f.stem, (-1.0, time.time()))
            if cpu - last_cpu > 0.01 * (time.time() - since) or ram < STALL_RAM_GB:
                progress[f.stem] = (cpu, time.time())
                if j.get("stalled_since"):
                    j.pop("stalled_since"); save(f, j)
                continue
            if not j.get("stalled_since") and time.time() - since > 600:
                j["stalled_since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)); save(f, j)
                log.write(f"{now()} {j['project']}/{j['name']} no CPU progress for 10 min, holds {ram:.1f} GB\n")
            if j.get("stall_min", 60) > 0 and time.time() - since > 60 * j.get("stall_min", 60):
                j["killed"] = f"stalled {j['stall_min']} min holding {ram:.1f} GB"
                save(f, j)
                log.write(f"{now()} killing {j['project']}/{j['name']}: {j['killed']}\n")
                kill_job(j)
        # reap exited children nobody waits for (jobs adopted across a RELOAD, background memory.reclaim writers)
        tracked = {p.pid for p in procs.values()}
        for d in os.listdir("/proc"):
            if d.isdigit() and int(d) not in tracked:
                try:
                    st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
                    if st[0] == "Z" and int(st[1]) == os.getpid():
                        os.waitpid(int(d), os.WNOHANG)
                except (OSError, IndexError, ValueError):
                    pass
        running = [j for _, j in jobs(RUNNING)]
        sweep_cgroups({j.get("cgroup") for j in running if j.get("cgroup")})
        threads = sum(j["threads"] for j in running)
        used = [job_usage(j)[0] for j in running]
        free_now = mem_available_gb()
        avail = free_now - OVERBOOK * sum(max(0.0, charge(j) - u) for j, u in zip(running, used) if not j.get("frozen"))
        def frozen_h(j):
            return (time.time() - time.mktime(time.strptime(j["frozen"], "%Y-%m-%d %H:%M:%S"))) / 3600
        drain = [j for j in running if j.get("frozen") and frozen_h(j) > DRAIN_AFTER_H]
        any_frozen = any(j.get("frozen") for j in running)
        empty_queue_free = free_now + sum(used)   # RAM free if every queue job ended (the rest belongs to others)
        # anti-starvation: a high-priority job, or one that waited STARVE_MIN for RAM or threads (and would fit in an
        # empty queue), is "urgent"; once an urgent job cannot start, no later job starts ahead of it (no backfill)
        def urgent(j):
            waited = time.time() - time.mktime(time.strptime(j["added"], "%Y-%m-%d %H:%M:%S"))
            # while a job is frozen RAM is already overbooked: a waiting big job must not also hold back the small ones
            return j.get("high") or (waited > 60 * STARVE_MIN and not any_frozen
                                     and empty_queue_free >= OVERBOOK * charge(j) + a.margin)
        waiting, blocker = {}, None
        paused = (Q / "PAUSE").exists()
        for f, j in sorted(jobs(PENDING), key=lambda fj: (not fj[1].get("high"), not urgent(fj[1]), fj[0].name)):
            why = None
            if paused:
                why = "PAUSE file"
            elif drain:
                why = f"{drain[0]['project']}/{drain[0]['name']} (frozen > {DRAIN_AFTER_H:g} h, machine draining for it)"
            elif blocker:
                why = f"{blocker} (waiting longer)"
            elif threads + j["threads"] > budget and threads > 0:
                why = f"threads ({threads} of {budget} in use)"
            elif avail < OVERBOOK * charge(j) + a.margin:
                why = f"memory (needs {OVERBOOK * charge(j) + a.margin:.1f} GB, {avail:.1f} GB free)"
            elif j["gpu"] > 0 and (time.time() - last_gpu_start < 180 or gpu_free_mb() < j["gpu"] + 500):
                # GPU memory is reported lazily by a just-started job: leave 3 min between GPU starts
                why = "GPU memory"
            if why:
                waiting[f.stem] = why
                if not paused and not blocker and urgent(j) and not why.startswith("GPU"):
                    blocker = f"{j['project']}/{j['name']}"
                continue
            if j["gpu"] > 0:
                last_gpu_start = time.time()
            env = dict(j.get("env") or os.environ)
            for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "JOBQ_THREADS"):
                env[k] = str(j["threads"])
            env["TORCHINDUCTOR_COMPILE_THREADS"] = "1"
            env["JOBQ_ID"] = f.stem
            inner = j["cmd"][0] if len(j["cmd"]) == 1 else shlex.join(j["cmd"])
            logf, rcf = LOGS / f"{f.stem}.log", LOGS / f"{f.stem}.rc"
            cg = cg_job(f.stem, ram_limit(j))
            if cg is not None:
                pre = f"echo $$ > {cg}/cgroup.procs; "
            elif j["gpu"] <= 0:
                # no cgroup: hard virtual-memory cap so a runaway job fails by itself instead of triggering the OOM
                # killer on another user's process (GPU jobs exempt: CUDA reserves a large virtual address space)
                pre = f"ulimit -v {int(ram_limit(j) * 1024 ** 2)}; "
            else:
                pre = ""
            shell = pre + "nice -n 19 ionice -c3 bash -c " + shlex.quote(inner) + f"; echo $? > {rcf}"
            try:
                p = subprocess.Popen(["bash", "-c", shell], cwd=j["cwd"], env=env, stdout=open(logf, "w"),
                                     stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                finish(f, j | {"started": now(), "log": str(logf)}, f"failed to start: {e}", log)
                continue
            j |= {"pid": p.pid, "started": now(), "log": str(logf), "cgroup": str(cg) if cg else None}
            save(RUNNING / f.name, j); f.unlink(); procs[f.stem] = p
            threads += j["threads"]; avail -= OVERBOOK * charge(j)
            log.write(f"{now()} start {j['project']}/{j['name']} pid {p.pid} thr {j['threads']} mem {j['mem']} "
                      f"cap {ram_limit(j):.1f} GB (free {avail + OVERBOOK * charge(j):.1f} GB, threads now {threads})\n")
        if tick % 10 == 0:
            hogs = scan_hogs(seen_hogs)
        tick += 1
        save(Q / "state.json", {"pid": os.getpid(), "heartbeat": time.time(), "budget": budget,
                                "cgroups": CG_ROOT is not None, "waiting": waiting, "hogs": hogs})
        try:
            tmp = Q / "QUEUE.md.tmp"
            tmp.write_text(render_md(status_dict(10)))
            tmp.replace(Q / "QUEUE.md")
        except Exception as e:
            log.write(f"{now()} QUEUE.md not written: {e}\n")
        if (Q / "RELOAD").exists():
            (Q / "RELOAD").unlink()
            log.write(f"{now()} reloading\n"); log.flush()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        for _ in range(6):     # memory guard every 5 s between the 30 s passes
            time.sleep(5)
            mem_guard(guard, log)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("add", help="queue a job")
    p.add_argument("--name", required=True)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--mem", type=float, default=2.0, help="peak RAM in GB (above 50 GB the rest goes to swap)")
    p.add_argument("--gpu", type=float, default=0.0, help="GPU memory in MB (0: no GPU)")
    p.add_argument("--high", action="store_true", help="high priority: starts before all normal jobs")
    p.add_argument("--ram", type=float, default=0.0, help="override the RAM cap in GB (beyond it the job swaps)")
    p.add_argument("--stall-min", type=int, default=60, help="kill after this many minutes without CPU use (0: never)")
    p.add_argument("--project", help="project tag (default: first folder of --cwd below $JOBQ_PROJECTS_ROOT)")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sp.add_parser("status", help="show the queue")
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="as Markdown (the daemon writes this to QUEUE.md every 30 s)")
    p.add_argument("--all", action="store_true", help="all finished jobs, not only the last 10")
    for c in ("cancel", "wait", "log"):
        p = sp.add_parser(c)
        p.add_argument("name", help="job name or id")
        p.add_argument("--project")
        if c == "wait":
            p.add_argument("--poll", type=int, default=30)
        if c == "log":
            p.add_argument("-n", "--lines", type=int, default=40)
    p = sp.add_parser("daemon")
    p.add_argument("--budget", type=int, default=0, help=f"thread budget (default {THREAD_BUDGET})")
    p.add_argument("--margin", type=float, default=MARGIN_GB)
    a = ap.parse_args()
    if a.cmd == "add" and a.command and a.command[0] == "--":
        a.command = a.command[1:]
    {"add": cmd_add, "status": cmd_status, "cancel": cmd_cancel, "wait": cmd_wait, "log": cmd_log,
     "daemon": cmd_daemon}[a.cmd](a)


if __name__ == "__main__":
    main()
