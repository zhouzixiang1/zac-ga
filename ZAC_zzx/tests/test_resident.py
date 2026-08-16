"""resident.py 单测（M1）：下次使用表 / 登记簿 / E2+容量逐出 / 三方案匹配 / 边界腿。

运行：ZAC/.venv/bin/python ZAC_zzx/tests/test_resident.py
（沿用 ZAC_new 的内联跑法——环境里没有 pytest）
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zac.ds.architecture import Architecture  # noqa: E402
from zzx.resident import (  # noqa: E402
    NextUse, ResidentRegistry, decide_lazy, match_return_sites, boundary_legs)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def make_arch():
    spec = json.load(open(ROOT / "hardware_spec/toy_architecture.json"))
    arch = Architecture(spec)
    arch.preprocessing()
    return arch


def make_registry(arch, n=10):
    # 原子铺在存储 SLM0（10×10）上，二维展开保证位置合法
    initial = [(0, i % 10, i // 10) for i in range(n)]
    return ResidentRegistry(arch, initial)


# ---------------------------------------------------------------- 1. NextUse
print("[1] NextUse 下次使用表")
sched = [[[0, 1], [2, 3]], [[1, 2]], [[0, 3], [4, 5]]]
nu = NextUse(sched)
check("next_round(1, after=0) = 1", nu.next_round(1, 0) == 1)
check("next_round(1, after=1) = None", nu.next_round(1, 1) is None)
check("next_round(0, after=0) = 2", nu.next_round(0, 0) == 2)
check("next_round(6, after=0) = None（从未上场）", nu.next_round(6, 0) is None)
check("partner_at(0,2) = 3", nu.partner_at(0, 2) == 3)
check("has_future_use(4, 0)", nu.has_future_use(4, 0))
check("not has_future_use(2, 1)", not nu.has_future_use(2, 1))

# ---------------------------------------------------------------- 2. Registry
print("[2] ResidentRegistry 登记簿")
arch = make_arch()
reg = make_registry(arch)
check("初始全存储", reg.occupancy() == 0 and len(reg.storage_site) == 10)
check("容量=区内总座位 42 (2×3×7)", reg.zone_sites == 42, f"got {reg.zone_sites}")
reg.enter_zone(0, (1, 0, 0))
reg.enter_zone(1, (2, 0, 0))
check("enter_zone 后占用 2", reg.occupancy() == 2)
check("座位对成对入区", reg.zone_seat[0] == (1, 0, 0) and reg.zone_seat[1] == (2, 0, 0))
check("congestion(0)=0（远未满）", reg.congestion(0) == 0.0)
anchor_loc, dist = reg.anchor(0, 0, nu)
check("anchor(0)：搭档 3 在存储 (0,3,0) → 原样返回", anchor_loc == (0, 3, 0) and dist == 2,
      f"got {anchor_loc},{dist}")
# 搭档 3 进区后锚点取其最近存储投影
reg.enter_zone(3, (1, 2, 6))
anchor_loc2, _ = reg.anchor(0, 0, nu)
proj = arch.nearest_storage_site(1, 2, 6)
check("anchor 投影规则：搭档在区内 → nearest_storage_site", anchor_loc2 == proj)

# ---------------------------------------------------------------- 3. E2 挡路逐出
print("[3] decide_lazy E2 挡路")
arch = make_arch()
reg = make_registry(arch)
nu2 = NextUse([[[6, 7], [8, 9]], [[6, 7]]])
# 驻留者 6 在 (1,0,0)；下一轮门 [6,7] 的座位对恰好要占 (1,0,0)/(2,0,0)
reg.enter_zone(6, (1, 0, 0))
reg.enter_zone(7, (2, 1, 1))
# 另一个非参与者驻留者 5 挡了第二个门的位
reg.enter_zone(5, (1, 2, 2))
next_gates = [[6, 7], [8, 9]]
seats = [((1, 0, 0), (2, 0, 0)), ((1, 2, 2), (2, 2, 2))]
dec, stats = decide_lazy(reg, nu2, 0, next_gates, seats)
check("参与者 7 STAY（即使坐在别人要的位——那是入区腿的事）",
      dec[7][0] == "STAY", f"got {dec.get(7)}")
check("非参与者 5 被挡路逐出", dec[5][0] == "RETURN" and stats["forced_e2"] == 1,
      f"got {dec.get(5)}, {stats}")
check("逐出者拿到合法存储位（SM L0 且不与在储原子重叠）",
      dec[5][1][0] == 0 and dec[5][1] not in set(
          tuple(s) for q, s in reg.storage_site.items() if q != 5))
check("登记簿同步：5 已回存储", not reg.is_resident(5) and reg.storage_site[5] == dec[5][1])
check("stats 计数", stats["participants"] == 4 and stats["return"] == 1)

# ---------------------------------------------------------------- 4. 容量阀逐出
print("[4] decide_lazy 容量阀")
arch = make_arch()
# 场景：23 个驻留者全坐第 6 列；下一轮 5 个外来门全坐第 0..4 列（E2 不触发）；
#       只有 q0..3 下次使用在 round2（最近），其余全是死驻留者。
#       θ=0.6 → 容量 25.2，需求 10 → 保留 ≤15 → 逐出 8 人，应全从死驻留者里出。
sched3 = ([[q, q + 1] for q in range(0, 20, 2)],
          [[q, q + 1] for q in range(40, 50, 2)],
          [[0, 1], [2, 3]])
nu3 = NextUse(sched3)
reg = make_registry(arch, 64)   # 测试用到 q49，登记簿要开到 50+
for q in range(20):
    reg.enter_zone(q, (1 + q % 2, q % 3, 6))
for q in (30, 31, 32):
    reg.enter_zone(q, (1 + q % 3, 2, 6))
next_gates = [[q, q + 1] for q in range(40, 50, 2)]
seats = [((1, i % 3, i), (2, i % 3, i)) for i in range(5)]
reg.theta = 0.6
dec, stats = decide_lazy(reg, nu3, 0, next_gates, seats)
check("E2 未触发（座位不重叠）", stats["forced_e2"] == 0, f"stats={stats}")
check("容量阀启动且逐出 ≥5", stats["capacity"] >= 5, f"stats={stats}")
ret = {q for q, v in dec.items() if v[0] == "RETURN"}
alive = {0, 1, 2, 3}
check("容量逐出只碰死驻留者（下次使用最近者幸存）",
      alive.isdisjoint(ret) and all(nu3.next_round(q, 0) is None for q in ret),
      f"evicted_alive={sorted(ret & alive)}")
check("容量约束满足：保留+需求 ≤ θ·容量",
      sum(1 for v in dec.values() if v[0] == "STAY") + 10 <= 0.6 * 42)

# ---------------------------------------------------------------- 5. 三方案匹配
print("[5] match_return_sites 三方案")
arch = make_arch()
reg = make_registry(arch)
nu5 = NextUse([[[0, 1]], [[1, 2]]])
reg.enter_zone(1, (1, 3, 3))                       # 搭档 1 在区中部
reg.enter_zone(0, (2, 0, 0))                       # 回返者 0：下次与 1 配对
sites = match_return_sites(reg, [0], nu5, 0)
site0 = sites[0]
home = reg.homes[0]
near = arch.nearest_storage_site(2, 0, 0)
partner_proj = arch.nearest_storage_site(1, 3, 3)
d_home = math.dist(arch.exact_SLM_location_tuple(site0), arch.exact_SLM_location_tuple(home))
d_near = math.dist(arch.exact_SLM_location_tuple(site0), arch.exact_SLM_location_tuple(near))
d_part = math.dist(arch.exact_SLM_location_tuple(site0), arch.exact_SLM_location_tuple(partner_proj))
print(f"    落位={site0}  Δhome={d_home:.1f} Δnear={d_near:.1f} Δpartner={d_part:.1f}")
check("落位在三方案张成的可行域内（离某族中心近）", min(d_home, d_near, d_part) < 2.5)
check("落位是自由位", site0 not in reg.occupied_storage() or site0 == home)
# 多回返者互异
reg2 = make_registry(arch)
for q in range(6):
    reg2.enter_zone(q, (1 + q % 2, q % 3, q))
sites2 = match_return_sites(reg2, list(range(6)), NextUse([]), 0)
check("多回返者互异", len(set(sites2.values())) == 6)
check("全部在存储 SLM", all(s[0] == 0 for s in sites2.values()))

# ---------------------------------------------------------------- 6. 边界腿
print("[6] boundary_legs 回相腿")
arch = make_arch()
reg = make_registry(arch)
nu6 = NextUse([[[0, 1]], []])
reg.enter_zone(0, (1, 0, 0))
reg.enter_zone(1, (2, 0, 0))
before = dict(reg.zone_seat)
dec, _ = decide_lazy(reg, nu6, 0, [], [], final_return_home=True)
legs = boundary_legs(before, dec, arch)
check("末边界全回收 → 2 条腿", len(legs) == 2)
check("腿格式 (dist,起x,起y,终x,终y) 且 dist>0",
      all(len(l) == 5 and l[0] > 0 for l in legs))
# 默认末边界（final_return_home=False）→ 全 STAY 0 腿
reg = make_registry(arch)
reg.enter_zone(0, (1, 0, 0))
before = dict(reg.zone_seat)
dec, stats = decide_lazy(reg, nu6, 0, [], [])
check("默认末边界全 STAY（native 同款）", dec[0][0] == "STAY" and boundary_legs(before, dec, arch) == [])

print(f"\n===== test_resident: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
