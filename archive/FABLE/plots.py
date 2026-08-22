"""FABLE 可视化：四张图看懂"复现 Fable 想法"的实验结果。

    fig1 逐电路三方对比条形图（时长比 vs ZAC，1.0 基准线）
    fig2 总览：geomean 时长比 + 平均保真度
    fig3 w_conf 消融曲线（qft_n29，冲突边权重的非单调性）
    fig4 GA v1a vs FABLE 逐电路散点（对角线=平手，线下方=FABLE 胜）

跑法：ZAC/.venv/bin/python FABLE/plots.py   （图存 FABLE/results/figs/）
"""
from __future__ import annotations

import csv
import json
import sys
from functools import reduce
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

FABLE_ROOT = Path(__file__).resolve().parent
ZAC_ROOT = FABLE_ROOT.parent
FIGS = FABLE_ROOT / "results/figs"
FIGS.mkdir(parents=True, exist_ok=True)

# macOS 中文字体（缺字回退 DejaVu）
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

C_FABLE, C_GA, C_NOTES, C_ZAC = "#d62728", "#1f77b4", "#2ca02c", "#7f7f7f"


def load_fidelity_dir(d: Path) -> dict[str, tuple[float, float]]:
    out = {}
    for fp in sorted(d.glob("*_fidelity.json")):
        with open(fp) as f:
            j = json.load(f)
        out[fp.name.split("_fidelity")[0].replace("_transpiled", "")] = (
            j["cir_duration"], j["cir_fidelity"])
    return out


def geomean(xs):
    return reduce(lambda a, b: a * b, xs) ** (1 / len(xs))


# ---------------------------------------------------------------- data
truth = {}
with open(ZAC_ROOT / "experiments/paper_truth/zac.csv") as f:
    for row in csv.DictReader(f):
        truth[row["circuit"]] = (float(row["duration_us"]), float(row["fidelity"]))
ours = load_fidelity_dir(FABLE_ROOT / "results/repro_fable/fidelity")
ga = load_fidelity_dir(ZAC_ROOT / "GA/results/repro_ga/fidelity")
notes = {}
with open(FABLE_ROOT / "paper_truth/fable_notes.csv") as f:
    for row in csv.DictReader(f):
        notes[row["circuit"]] = (float(row["zac_us"]), float(row["fable_search_us"]),
                                 float(row["zac_fid"]), float(row["fable_search_fid"]))

circuits = sorted(c for c in truth if c in ours)
r_ours = {c: ours[c][0] / truth[c][0] for c in circuits}
r_ga = {c: ga[c][0] / truth[c][0] for c in circuits if c in ga}
r_notes = {c: notes[c][1] / notes[c][0] for c in circuits}

# ---------------------------------------------------------------- fig1
order = sorted(circuits, key=lambda c: r_ours[c], reverse=True)
y = range(len(order))
h = 0.27
fig, ax = plt.subplots(figsize=(9, 7.5))
ax.barh([i + h for i in y], [r_notes[c] for c in order], h, label="Fable 原生(笔记)", color=C_NOTES, alpha=.85)
ax.barh(list(y), [r_ours[c] for c in order], h, label="FABLE(本实验)", color=C_FABLE)
ax.barh([i - h for i in y], [r_ga[c] for c in order], h, label="GA v1a", color=C_GA, alpha=.85)
ax.axvline(1.0, color="k", lw=1, ls="--")
ax.text(1.005, len(order) - 0.4, "ZAC 基准 1.0", fontsize=9)
ax.set_yticks(list(y))
ax.set_yticklabels(order, fontsize=9)
ax.invert_yaxis()
ax.set_xlabel("重排+执行总时长相对 ZAC 的比值（越小越快）")
ax.set_title("逐电路对比：Fable 想法装上 ZAC 底盘 (18 电路)")
ax.legend(loc="lower right")
fig.tight_layout()
fig.savefig(FIGS / "fig1_per_circuit.png", dpi=160)

# ---------------------------------------------------------------- fig2
gm = [1.0, geomean(list(r_ga.values())), geomean(list(r_ours.values())),
      geomean(list(r_notes.values()))]
fid = [sum(truth[c][1] for c in circuits) / len(circuits),
       sum(ga[c][1] for c in r_ga) / len(r_ga),
       sum(ours[c][1] for c in circuits) / len(circuits),
       sum(notes[c][3] for c in circuits) / len(circuits)]
labels = ["ZAC 真值\n(HPCA'25)", "GA v1a\n(ICCAD评估)", "FABLE\n(本实验)", "Fable 原生\n(笔记)"]
fig, (a, b) = plt.subplots(1, 2, figsize=(10, 4))
colors = [C_ZAC, C_GA, C_FABLE, C_NOTES]
bars = a.bar(labels, gm, color=colors, alpha=.9)
for r, v in zip(bars, gm):
    a.text(r.get_x() + r.get_width() / 2, v + .002, f"{v:.3f}", ha="center", fontsize=10)
a.axhline(1.0, color="k", lw=.8, ls="--")
a.set_ylabel("时长 geomean 比 vs ZAC（越小越好）")
a.set_title("总时长")
a.set_ylim(0.9, 1.02)
bars = b.bar(labels, fid, color=colors, alpha=.9)
for r, v in zip(bars, fid):
    b.text(r.get_x() + r.get_width() / 2, v + .0002, f"{v:.4f}", ha="center", fontsize=10)
b.set_ylabel("平均保真度（越高越好）")
b.set_title("平均保真度")
b.set_ylim(0.474, 0.486)
fig.suptitle("总览：FABLE 0.942× 追平 Fable 原生 0.938×，均优于 ZAC 与 GA v1a")
fig.tight_layout()
fig.savefig(FIGS / "fig2_summary.png", dpi=160)

# ---------------------------------------------------------------- fig3
QFT29_ZAC = 46256.9


def read_abl(tag: str) -> float:
    """读一个 qft_n29 消融结果的时长比。tag=repro_fable_wc100 对应 w_conf=1.0。"""
    fp = FABLE_ROOT / f"results/{tag}/fidelity/qft_n29_transpiled_fidelity.json"
    return json.load(open(fp))["cir_duration"] / QFT29_ZAC


xs = [0.0, 0.25, 1.0, 3.0]
ys = [read_abl(t) for t in ("abl_qft29_wc0", "abl_qft29_wc025",
                            "repro_fable_wc100", "abl_qft29_wc3")]
fig, ax = plt.subplots(figsize=(6.5, 4.2))
ax.plot(xs, ys, "o-", color=C_FABLE, lw=2, ms=8, label="带复用前瞻")
ax.axhline(1.0, color="k", lw=.8, ls="--")
no_la = read_abl("abl_qft29_wc0_noLA")
ax.scatter([0.0], [no_la], marker="s", color=C_GA, s=70, zorder=5,
           label="无前瞻 (w_conf=0)")
ax.scatter([ga["qft_n29"][0] / QFT29_ZAC], [r_ga["qft_n29"]],
           marker="^", color="k", s=80, zorder=5, label="GA v1a 参照")
for x, v in zip(xs, ys):
    ax.annotate(f"{v:.2f}×", (x, v), textcoords="offset points", xytext=(0, 9),
                ha="center", fontsize=10)
ax.annotate("甜点 0.25", (0.25, ys[1]), textcoords="offset points",
            xytext=(12, -18), fontsize=11, color=C_FABLE)
ax.set_xlabel("冲突边权重 w_conf")
ax.set_ylabel("qft_n29 时长比 vs ZAC")
ax.set_title("冲突边权重的非单调性（qft_n29 消融）")
ax.legend(loc="upper left")
fig.tight_layout()
fig.savefig(FIGS / "fig3_wconf_ablation.png", dpi=160)

# ---------------------------------------------------------------- fig4
fig, ax = plt.subplots(figsize=(6.5, 5.5))
common = [c for c in circuits if c in r_ga]
ax.plot([0.4, 1.5], [0.4, 1.5], "k--", lw=.8)
ax.fill_between([0.4, 1.5], [0.4, 1.5], [1.5, 1.5], alpha=.06, color=C_FABLE)
ax.fill_between([0.4, 1.5], [0.4, 0.4], [0.4, 1.5], alpha=.06, color=C_GA)
ax.scatter([r_ga[c] for c in common], [r_ours[c] for c in common],
           s=55, color=C_FABLE, zorder=5)
for c in common:
    ax.annotate(c, (r_ga[c], r_ours[c]), textcoords="offset points",
                xytext=(5, 4), fontsize=8)
ax.text(1.32, 1.12, "FABLE 更快", fontsize=11, color=C_FABLE)
ax.text(0.52, 0.98, "GA 更快", fontsize=11, color=C_GA)
ax.set_xlabel("GA v1a 时长比 vs ZAC")
ax.set_ylabel("FABLE 时长比 vs ZAC")
ax.set_title("换上 Fable 评估+算子后谁赢了？（对角线=平手）")
ax.set_xlim(0.45, 1.45)
ax.set_ylim(0.55, 1.35)
fig.tight_layout()
fig.savefig(FIGS / "fig4_ga_vs_fable.png", dpi=160)

print("figures saved to", FIGS)
for p in sorted(FIGS.glob("*.png")):
    print(" ", p.name)
