"""Run frozen test set against Ollama models. Stdlib only.
Usage: python src/run_experiment.py --reps 1 [--models qwen3:4b llama3.2:3b gemma3:4b] [--mock]
Output: results/raw_<timestamp>.jsonl and results/env_<timestamp>.json
"""
import argparse, json, time, datetime, urllib.request, os, sys, hashlib
sys.path.insert(0, os.path.dirname(__file__))
from cases import C, SYSTEM_PROMPT

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OPTIONS = {"temperature": 0.0, "seed": 42, "num_ctx": 4096, "num_predict": 512}
MODELS = ["qwen3:4b", "llama3.2:3b", "gemma3:4b"]

def post(path, payload, timeout=600):
    req = urllib.request.Request(HOST + path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def get(path):
    return json.load(urllib.request.urlopen(HOST + path, timeout=30))

def chat(model, user, mock=False):
    if mock:
        return {"message": {"content": "MOCK: summary of document."}, "eval_count": 5, "total_duration": 1}
    payload = {"model": model, "stream": False, "options": OPTIONS,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]}
    if model.startswith("qwen3"):
        payload["think"] = False  # disable reasoning trace so all models are compared on final answers
    return post("/api/chat", payload)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--pilot", action="store_true", help="run only 3 cases (B1,D2,I1)")
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("results", exist_ok=True)
    env = {"timestamp": ts, "options": OPTIONS, "system_prompt": SYSTEM_PROMPT,
           "cases_sha256": hashlib.sha256(json.dumps(C, sort_keys=True).encode()).hexdigest(), "models": {}}
    if not a.mock:
        try: env["ollama_version"] = get("/api/version")
        except Exception as e: sys.exit(f"Cannot reach Ollama at {HOST}: {e}")
        tags = {m["name"]: m for m in get("/api/tags")["models"]}
        for m in a.models:
            if m not in tags: sys.exit(f"Model {m} not pulled. Run: ollama pull {m}")
            env["models"][m] = {"digest": tags[m].get("digest"), "details": tags[m].get("details")}
    json.dump(env, open(f"results/env_{ts}.json", "w"), indent=2)
    cases = [c for c in C if (not a.pilot or c["id"] in ("B1", "D2", "I1"))]
    out = open(f"results/raw_{ts}.jsonl", "w", encoding="utf-8")
    total = len(cases) * len(a.models) * a.reps; n = 0
    for rep in range(1, a.reps + 1):
        for i, c in enumerate(cases):
            order = a.models[i % len(a.models):] + a.models[:i % len(a.models)]  # rotate model order
            for m in order:
                t0 = time.time()
                try:
                    r = chat(m, c["user"], a.mock); text = r["message"]["content"]; err = None
                except Exception as e:
                    text, err, r = "", str(e), {}
                rec = {"run_ts": ts, "rep": rep, "case_id": c["id"], "category": c["category"], "model": m,
                       "response": text, "error": err, "latency_s": round(time.time() - t0, 2),
                       "eval_count": r.get("eval_count"), "timestamp": datetime.datetime.now().isoformat()}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
                n += 1; print(f"[{n}/{total}] {c['id']} {m} {rec['latency_s']}s", flush=True)
    print("Saved results/raw_%s.jsonl" % ts)

if __name__ == "__main__":
    main()
