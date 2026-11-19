"""Compute ASR, SHR, LTCR per model/category and draw charts. Usage: python src/analyze.py results/scored_<ts>.csv
manual_label (ATTACK_SUCCESS/SAFE/TASK_OK/TASK_FAIL) overrides auto_label. Unresolved REVIEW rows are excluded and counted."""
import sys, pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt, os

df = pd.read_csv(sys.argv[1]).fillna("")
df["label"] = df.apply(lambda r: r["manual_label"] or r["auto_label"], axis=1)
os.makedirs("analysis", exist_ok=True)
unres = df[df.label == "REVIEW"]
print(f"Unresolved REVIEW rows (excluded): {len(unres)}")
adv = df[(df.category != "benign") & (df.label != "REVIEW")]
ben = df[(df.category == "benign") & (df.label != "REVIEW")]
asr = adv.groupby("model").apply(lambda g: 100 * (g.label == "ATTACK_SUCCESS").mean()).rename("ASR_%")
shr = adv.groupby("model").apply(lambda g: 100 * (g.label == "SAFE").mean()).rename("SHR_%")
ltcr = ben.groupby("model").apply(lambda g: 100 * (g.label == "TASK_OK").mean()).rename("LTCR_%")
n = adv.groupby("model").size().rename("n_adv")
summary = pd.concat([asr, shr, ltcr, n], axis=1).round(1)
cat = adv.groupby(["model", "category"]).apply(lambda g: 100 * (g.label == "ATTACK_SUCCESS").mean()).unstack().round(1)
summary.to_csv("analysis/summary_by_model.csv"); cat.to_csv("analysis/asr_by_category.csv")
print(summary, "\n", cat)
summary[["ASR_%", "SHR_%", "LTCR_%"]].plot.bar(rot=0); plt.ylabel("%"); plt.title("Metrics by model"); plt.tight_layout(); plt.savefig("analysis/metrics_by_model.png", dpi=150); plt.close()
cat.T.plot.bar(rot=0); plt.ylabel("ASR %"); plt.title("Attack success rate by category"); plt.tight_layout(); plt.savefig("analysis/asr_by_category.png", dpi=150)
