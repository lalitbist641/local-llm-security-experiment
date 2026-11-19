"""Live dashboard + controller for the experiment. Stdlib only.
Run:  python src/dashboard.py            then open http://localhost:8765
Mock: python src/dashboard.py --mock     (fake models, to try the UI without Ollama)
"""
import io, json, os, sys, time, threading, datetime, hashlib, csv, io, random, argparse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cases import C, SYSTEM_PROMPT
import score as scorer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results"); os.makedirs(RES, exist_ok=True)
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
ALL_MODELS = ["qwen3:4b", "llama3.2:3b", "gemma3:4b"]
CASE_BY_ID = {c["id"]: c for c in C}
MOCK = False
L = threading.Lock()
S = {"status": "idle", "config": {}, "jobs_total": 0, "jobs_done": 0, "current": None, "results": [],
     "log": [], "started": None, "ended": None, "run_ts": None, "error": None, "env": {}}
pause_ev = threading.Event(); pause_ev.set()
stop_flag = threading.Event()

def log(msg):
    with L:
        S["log"].append(f"{datetime.datetime.now().strftime('%H:%M:%S')}  {msg}")
        S["log"] = S["log"][-200:]

def http_json(path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(HOST + path, data, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def stream_chat(model, user, opts, on_token):
    """Returns (full_text, eval_count, eval_duration_ns). Streams tokens to on_token."""
    if MOCK:
        time.sleep(0.3)
        bad = model.startswith("llama") and random.random() < 0.6
        c = next(c for c in C if c["user"] == user)
        if bad and c["category"] != "benign": txt = f"Sure! {c.get('canary') or 'RESEARCH_SECRET_7391'} here you go."
        elif c["category"] == "benign" or c["category"] == "indirect": txt = "Summary: the Kuala Lumpur depot cut delivery time; 14 May 10:00 Room B; three days; mixed review; priya room b ahmed inventory; backup disk 85 warn; order damaged late refund."
        else: txt = "I can't share confidential information."
        out = ""
        for w in txt.split(" "):
            if stop_flag.is_set(): break
            out += w + " "; on_token(out); time.sleep(0.06)
        return out.strip(), len(out.split()), 1e9
    payload = {"model": model, "stream": True, "options": opts,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]}
    if model.startswith("qwen3"): payload["think"] = False
    def open_req(p):
        return urllib.request.urlopen(urllib.request.Request(HOST + "/api/chat", json.dumps(p).encode(), {"Content-Type": "application/json"}), timeout=900)
    try: resp = open_req(payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        if "think" in body.lower() and "think" in payload:
            payload.pop("think"); log("Ollama rejected 'think' flag; retrying without it (update Ollama for fair qwen3 comparison)")
            resp = open_req(payload)
        else: raise RuntimeError(f"HTTP {e.code}: {body[:200]}")
    out, ec, ed = "", None, None
    for line in resp:
        if stop_flag.is_set(): break
        if not line.strip(): continue
        j = json.loads(line)
        out += j.get("message", {}).get("content", ""); on_token(out)
        if j.get("done"): ec, ed = j.get("eval_count"), j.get("eval_duration")
    return out, ec, ed

def worker(cfg):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    opts = {"temperature": cfg["temperature"], "num_ctx": cfg["num_ctx"], "num_predict": cfg["num_predict"]}
    if cfg.get("seed") not in (None, ""): opts["seed"] = int(cfg["seed"])
    cases = [c for c in C if (not cfg["pilot"] or c["id"] in ("B1", "D2", "I1"))]
    models = cfg["models"]
    jobs = []
    for rep in range(1, cfg["reps"] + 1):
        for i, c in enumerate(cases):
            k = i % len(models)
            for m in models[k:] + models[:k]: jobs.append((rep, c, m))
    env = {"timestamp": ts, "options": opts, "system_prompt": SYSTEM_PROMPT, "config": cfg, "mock": MOCK,
           "cases_sha256": hashlib.sha256(json.dumps(C, sort_keys=True).encode()).hexdigest()}
    if not MOCK:
        try:
            env["ollama_version"] = http_json("/api/version")
            tags = {m["name"]: m for m in http_json("/api/tags")["models"]}
            env["models"] = {m: {"digest": tags[m]["digest"], "details": tags[m].get("details")} for m in models if m in tags}
        except Exception as e: log(f"could not record env: {e}")
    json.dump(env, open(os.path.join(RES, f"env_{ts}.json"), "w"), indent=2)
    with L: S["env"] = env
    raw = open(os.path.join(RES, f"raw_{ts}.jsonl"), "w", encoding="utf-8")
    with L:
        S.update(status="running", jobs_total=len(jobs), jobs_done=0, results=[], started=time.time(), ended=None, run_ts=ts, error=None)
    log(f"Started run {ts}: {len(jobs)} generations ({len(cases)} cases x {len(models)} models x {cfg['reps']} reps)")
    for rep, c, m in jobs:
        pause_ev.wait()
        if stop_flag.is_set(): break
        t0 = time.time()
        with L: S["current"] = {"case_id": c["id"], "category": c["category"], "model": m, "rep": rep, "user": c["user"], "partial": "", "t0": t0}
        def on_token(txt):
            with L:
                if S["current"]: S["current"]["partial"] = txt
        try:
            text, ec, ed = stream_chat(m, c["user"], opts, on_token); err = None
        except Exception as e:
            text, ec, ed, err = "", None, None, str(e); log(f"ERROR {c['id']} {m}: {e}")
        if stop_flag.is_set() and not err: break  # discard the interrupted generation
        rec = {"run_ts": ts, "rep": rep, "case_id": c["id"], "category": c["category"], "model": m, "response": text,
               "error": err, "latency_s": round(time.time() - t0, 2), "eval_count": ec,
               "tok_s": round(ec / (ed / 1e9), 1) if ec and ed else None, "timestamp": datetime.datetime.now().isoformat()}
        lab, why = scorer.score(rec)
        rec.update(auto_label=lab, auto_reason=why, manual_label="", manual_note="")
        raw.write(json.dumps(rec, ensure_ascii=False) + "\n"); raw.flush()
        with L:
            S["results"].append(rec); S["jobs_done"] += 1
        log(f"{c['id']} {m} -> {lab} ({rec['latency_s']}s)")
    raw.close()
    with L:
        S["current"] = None; S["ended"] = time.time(); S["status"] = "stopped" if stop_flag.is_set() else "done"
    log("Run " + S["status"])

def label_of(r): return r["manual_label"] or r["auto_label"]

def metrics():
    with L: rs = list(S["results"])
    out = {"models": {}, "cats": {}, "pending_review": sum(1 for r in rs if label_of(r) == "REVIEW")}
    for m in sorted({r["model"] for r in rs}):
        mr = [r for r in rs if r["model"] == m]
        adv = [r for r in mr if r["category"] != "benign" and label_of(r) != "REVIEW"]
        ben = [r for r in mr if r["category"] == "benign" and label_of(r) != "REVIEW"]
        pct = lambda a, b: round(100 * a / b, 1) if b else None
        out["models"][m] = {"n": len(mr), "n_adv": len(adv), "n_ben": len(ben),
            "ASR": pct(sum(label_of(r) == "ATTACK_SUCCESS" for r in adv), len(adv)),
            "SHR": pct(sum(label_of(r) == "SAFE" for r in adv), len(adv)),
            "LTCR": pct(sum(label_of(r) == "TASK_OK" for r in ben), len(ben)),
            "avg_s": round(sum(r["latency_s"] for r in mr) / len(mr), 1),
            "avg_tok": round(sum(r["tok_s"] for r in mr if r.get("tok_s")) / max(1, sum(1 for r in mr if r.get("tok_s"))), 1),
            "dist": {l: sum(label_of(r) == l for r in mr) for l in ("ATTACK_SUCCESS", "SAFE", "TASK_OK", "TASK_FAIL", "REVIEW")}}
        out["cats"][m] = {}
        for cat in ("direct", "conflicting", "override", "indirect"):
            g = [r for r in adv if r["category"] == cat]
            out["cats"][m][cat] = {"asr": pct(sum(label_of(r) == "ATTACK_SUCCESS" for r in g), len(g)), "n": len(g)}
    return out

def state_json():
    with L:
        s = {k: S[k] for k in ("status", "config", "jobs_total", "jobs_done", "log", "started", "ended", "run_ts", "error")}
        cur = dict(S["current"]) if S["current"] else None
        s["results"] = [{**{k: r[k] for k in ("case_id", "category", "model", "rep", "auto_label", "auto_reason", "manual_label", "manual_note", "latency_s", "tok_s", "error")}, "idx": i} for i, r in enumerate(S["results"])]
    if cur: cur["elapsed"] = round(time.time() - cur.pop("t0"), 1)
    s["current"] = cur; s["metrics"] = metrics(); s["paused"] = not pause_ev.is_set(); s["now"] = time.time()
    return s

def ollama_info():
    if MOCK: return {"ok": True, "version": "mock", "models": ALL_MODELS, "running": []}
    try:
        v = http_json("/api/version", timeout=3)["version"]
        tags = [m["name"] for m in http_json("/api/tags", timeout=3)["models"]]
        ps = [{"name": m["name"], "gpu_pct": round(100 * m.get("size_vram", 0) / m["size"]) if m.get("size") else None,
               "size_gb": round(m.get("size", 0) / 1e9, 2)} for m in http_json("/api/ps", timeout=3).get("models", [])]
        return {"ok": True, "version": v, "models": tags, "running": ps}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def csv_bytes():
    with L: rs = list(S["results"])
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["case_id", "category", "model", "rep", "final_label", "auto_label", "auto_reason", "manual_label", "manual_note", "latency_s", "tok_s", "response"])
    for r in rs: w.writerow([r["case_id"], r["category"], r["model"], r["rep"], label_of(r), r["auto_label"], r["auto_reason"], r["manual_label"], r["manual_note"], r["latency_s"], r.get("tok_s"), r["response"]])
    return buf.getvalue().encode("utf-8-sig")

def xlsx_bytes():
    import report_xlsx  # needs openpyxl
    with L: rs = [dict(r) for r in S["results"]]; env = dict(S["env"])
    return report_xlsx.build_report(rs, env, C)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, obj, code=200, ctype="application/json"):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/": self.send(HTML, ctype="text/html; charset=utf-8")
        elif p == "/api/state": self.send(state_json())
        elif p == "/api/ollama": self.send(ollama_info())
        elif p == "/api/cases": self.send([{"id": c["id"], "category": c["category"], "user": c["user"]} for c in C])
        elif p == "/api/download":
            fmt = (self.path.split("fmt=")[1].split("&")[0] if "fmt=" in self.path else "xlsx")
            with L: ts = S["run_ts"] or "run"; has = bool(S["results"])
            if not has: return self.send({"error": "No results yet"}, 400)
            try:
                if fmt == "csv": data, ct, name = csv_bytes(), "text/csv; charset=utf-8", f"LLM_Security_Results_{ts}.csv"
                else: data, ct, name = xlsx_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"LLM_Security_Report_{ts}.xlsx"
            except ImportError: return self.send({"error": "Excel export needs openpyxl. Run:  pip install openpyxl"}, 500)
            except Exception as e: return self.send({"error": f"export failed: {e}"}, 500)
            self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"'); self.send_header("Access-Control-Expose-Headers", "Content-Disposition"); self.end_headers(); self.wfile.write(data)
        elif p.startswith("/api/result/"):
            i = int(p.rsplit("/", 1)[1])
            with L: r = S["results"][i] if 0 <= i < len(S["results"]) else None
            self.send(r or {}, 200 if r else 404)
        else: self.send({"error": "not found"}, 404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}")
        p = self.path
        if p == "/api/start":
            if S["status"] in ("running",): return self.send({"error": "already running"}, 409)
            cfg = {"models": [m for m in body.get("models", ALL_MODELS) if m], "reps": int(body.get("reps", 1)), "pilot": bool(body.get("pilot")),
                   "temperature": float(body.get("temperature", 0)), "seed": body.get("seed", 42), "num_ctx": int(body.get("num_ctx", 4096)), "num_predict": int(body.get("num_predict", 512))}
            if not cfg["models"]: return self.send({"error": "no models"}, 400)
            stop_flag.clear(); pause_ev.set(); S["config"] = cfg
            threading.Thread(target=worker, args=(cfg,), daemon=True).start(); self.send({"ok": True})
        elif p == "/api/pause": pause_ev.clear(); log("Paused (finishes current generation first)"); self.send({"ok": True})
        elif p == "/api/resume": pause_ev.set(); log("Resumed"); self.send({"ok": True})
        elif p == "/api/stop": stop_flag.set(); pause_ev.set(); log("Stop requested"); self.send({"ok": True})
        elif p == "/api/label":
            with L:
                r = S["results"][int(body["idx"])]; r["manual_label"] = body.get("label", ""); r["manual_note"] = body.get("note", "")
            self.send({"ok": True})
        else: self.send({"error": "not found"}, 404)

HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8").read()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--mock", action="store_true"); ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(); MOCK = a.mock
    print(f"Dashboard: http://localhost:{a.port}   (Ctrl+C to quit)" + ("  [MOCK MODE]" if MOCK else ""))
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
