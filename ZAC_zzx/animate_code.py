"""用 ZAC 原版动画器渲染编译产物：code JSON → 原子搬运 mp4。

不重新编译——直接吃 results/*/code/*.json（zzx 与 ZAC/B 产物同格式，
动画器按时间窗查指令，zzx 相位分裂导致的指令 id 乱序不受影响）。

用法：
  python3 animate_code.py results/main/code/ising_n42_code.json \
      [--arch hardware_spec/zac_arch_repro.json] [--out out.mp4]
  python3 animate_code.py a.json b.json --out-dir results/anim   # 批量，按文件名落盘

画面元素（ZAC 原版约定）：
  绿圈 = SLM 座位（宿舍铺位+车间工位）  黑点 = 原子
  红色横/竖虚线 = AOD 光镊的行/列（搬运时点亮）
  蓝色矩形 = 激发区开灯（Rydberg 门批，慢镜头播放）
  圆圈标记 = 正在做 1q 门的原子
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture   # noqa: E402
from zac.animator.animator import Animator     # noqa: E402


def render(code_path: str, arch_spec: str, out: str):
    with open(ROOT / arch_spec if not Path(arch_spec).is_absolute() else arch_spec) as f:
        arch = Architecture(json.load(f))
    arch.preprocessing()
    code = json.load(open(code_path))
    # Animator 是 ZAC 编译器的混入类；单独使用时把架构递给它即可
    an = Animator()
    an.architecture = arch
    an.animate(code, output=out)
    print(f"✅ {code_path} → {out}  (runtime {code['runtime']:.1f}μs)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+", help="code JSON 路径（一个或多个）")
    ap.add_argument("--arch", default="hardware_spec/zac_arch_repro.json")
    ap.add_argument("--out", default=None, help="单个文件时的输出 mp4 路径")
    ap.add_argument("--out-dir", default=None, help="批量模式：输出目录，按电路名命名")
    a = ap.parse_args()
    if len(a.codes) > 1 and a.out:
        ap.error("--out 只能与单个输入连用")
    for cp in a.codes:
        p = Path(cp).resolve()
        stem = p.stem.replace("_code", "")
        if p.parent.name == "code":          # results/<系统>/code/x.json → <系统>_x
            stem = f"{p.parent.parent.name}_{stem}"
        if a.out_dir:
            Path(a.out_dir).mkdir(parents=True, exist_ok=True)
            out = f"{a.out_dir}/{stem}.mp4"
        else:
            out = a.out or f"{ROOT / 'results' / 'anim' / (stem + '.mp4')}"
            Path(out).parent.mkdir(parents=True, exist_ok=True)
        render(cp, a.arch, out)
