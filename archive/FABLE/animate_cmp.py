"""生成 ZAC 原版 vs FABLE 的原子搬运动画（mp4）并给出定量对比。

同电路两份动画（同一硬件 spec、同一时间轴尺度），直观看放置搜索
带来的差异：重排批次数、每批最长搬运腿、留守复用 vs 回宿舍。

用法：ZAC/.venv/bin/python FABLE/animate_cmp.py [circuit ...]
（默认 swap_test_n25 与 seca_n11 —— FABLE 赢 ZAC 15%/17% 的两个电路）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from zac.ds.architecture import Architecture
from zac.simulator.simulator import Simulator  # noqa: F401  (确认环境完整)

from fable.zac_fable import ZAC_FABLE

FABLE_ROOT = Path(__file__).resolve().parent
ZAC_ROOT = FABLE_ROOT.parent

# (电路文件名基名, ZAC 基线 ZAIR 路径, FABLE ZAIR 路径)
SOURCES = {
    "swap_test_n25_transpiled": (
        ZAC_ROOT / "ZAC/result/zac/repro_fixed/code/swap_test_n25_transpiled_code.json",
        FABLE_ROOT / "results/repro_fable/code/swap_test_n25_transpiled_code.json"),
    "seca_n11_transpiled": (
        ZAC_ROOT / "ZAC/result/zac/repro_fixed/code/seca_n11_transpiled_code.json",
        FABLE_ROOT / "results/repro_fable/code/seca_n11_transpiled_code.json"),
}


def load_stats(code_path: Path) -> dict:
    d = json.load(open(code_path))
    jobs = [i for i in d["instructions"] if i["type"] == "rearrangeJob"]
    spans = [i["end_time"] - i["begin_time"] for i in jobs]
    transfers = sum(1 for i in jobs for det in i.get("insts", [])
                    if det.get("type", "").startswith(("activate", "deactivate")))
    return {
        "rearr_jobs": len(jobs),
        "rearr_span_sum_us": round(sum(spans), 1),
        "longest_job_us": round(max(spans), 1) if spans else 0.0,
        "transfers": transfers,
        "runtime_us": round(d.get("runtime", 0), 1),
    }


def main() -> None:
    names = sys.argv[1:] or list(SOURCES)
    with open(FABLE_ROOT / "hardware_spec/zac_arch_repro.json") as f:
        spec = json.load(f)
    arch = Architecture(spec)
    arch.preprocessing()
    out_dir = FABLE_ROOT / "results/figs/anim"
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in names:
        zac_path, fable_path = SOURCES[name]
        short = name.replace("_transpiled", "")
        print(f"===== {short} =====")
        for tag, path in [("zac", zac_path), ("fable", fable_path)]:
            code = json.load(open(path))
            out = out_dir / f"{tag}_{short}.mp4"
            # 动画器是 ZAC 类的 mixin：只要 architecture + code 字典即可渲染，
            # 不用重跑编译（两份 ZAIR 都是已落盘的确定性结果）
            compiler = ZAC_FABLE()
            compiler.set_architecture(arch)
            compiler.animate(code, output=str(out))
            s = load_stats(path)
            print(f"  [{tag:5}] {s}")
            print(f"          -> {out.name}")


if __name__ == "__main__":
    main()
