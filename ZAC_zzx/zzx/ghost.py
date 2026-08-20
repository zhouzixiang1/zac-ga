"""鬼点判据模块（前瞻 v2 的公共部件：硬保证层 / 软罚 / 验证器⑨共用）。

物理模型
--------
一个搬运批激活若干**行光束**（全程宽）与**列光束**（全程高），陷阱在
所有 (列,行) 交叉点上——包括没载原子的"空交叉点"（i≠j 的组合也算）。
光束各自线性平移，交叉点随之扫过平面。静止原子（鬼点）被撞 ⇔
某列光束扫过其 x 的时刻与某行光束扫过其 y 的时刻重合：

    命中 ⇔ valid(s_x) ∧ valid(s_y) ∧ (s_x≡always ∨ s_y≡always ∨ s_x==s_y)

其中 s = (ghost − 起)/(终 − 起) ∈ [0,1]；光束全程静止且恰在 ghost 的
坐标上记为 always。s=0/s=1 天然覆盖 activate/deactivate 两端的定点时刻。

与四规则的关系：compatible_2d（zcost.py）管"动 vs 动"——两台都被
夹住的原子不许交叉/并线；本模块管"动 vs 静"——车扫过路边的人。
两篇论文（ZAC / ICCAD'25）的约束清单都写了"鬼点（行列所有网格点
受影响）"，但两家代码的冲突图都只收位置变化的原子（ZAC
router.py:56-61 / qmap getAtomsToMove），静止原子从未参检。

保守联合口径（硬保证层的依据）
------------------------------
鬼点要求贡献列的腿与贡献行的腿**同批同时激活**。把一个相位的全部腿
当成同时激活来检查，是任何路由分批（图着色）结果的严格上界：
放置期按联合口径查零鬼点 ⇒ 路由期任意分批后仍零鬼点（分批只会
减少同时存在的光束对，不会增加）。

腿的格式与 zcost.py/fcost.py 一致：(dist, 起x, 起y, 终x, 终y)。
"""
from __future__ import annotations

EPS = 1e-9
S_TOL = 1e-6          # 时刻相等的容差（浮点参数化误差）


def beams(legs):
    """腿集 → (列轨迹表, 行轨迹表)。每条腿贡献一条列 (起x→终x) 和一条行 (起y→终y)。

    保留重复（两条腿同列轨迹意味着两条光束，但判定只看存在性，去重无碍），
    去重可把后续循环缩小 2-4 倍——这里直接 set 化。
    """
    cols, rows = set(), set()
    for d, sx, sy, tx, ty in legs:
        cols.add((sx, tx))
        rows.add((sy, ty))
    return sorted(cols), sorted(rows)


def sweep_bbox(cols, rows):
    """所有光束扫过的联合包围盒 (xmin, xmax, ymin, ymax)；空集返回 None。

    盒外的静止原子不可能被撞（列够不着其 x 或行够不着其 y）。
    """
    if not cols or not rows:
        return None
    xs = [x for c in cols for x in c]
    ys = [y for r in rows for y in r]
    return min(xs), max(xs), min(ys), max(ys)


def _cover(traj, g):
    """单条光束对坐标 g 的覆盖：None=光束全程静止且恰在 g（always）；
    浮点 s∈[0,1]=第 s 时刻扫过；invalid=返回 None 外的哨兵——用
    (False, 0.0) 与 (True, s)/(True, None) 区分。"""
    a, b = traj
    if abs(b - a) < EPS:                      # 静止光束
        return (True, None) if abs(g - a) < EPS else (False, 0.0)
    s = (g - a) / (b - a)
    if -EPS <= s <= 1.0 + EPS:
        return True, min(max(s, 0.0), 1.0)
    return False, 0.0


def ghost_hits(legs, ghosts, detail=False):
    """精确鬼点检查。

    Args:
        legs:    本批（或本相位联合口径）的全部腿 (dist, 起x, 起y, 终x, 终y)
        ghosts:  静止原子列表 [(id, x, y)]
        detail:  False 返回 [(鬼id,x,y)]；True 附加肇事光束与时刻
                 [(鬼id,x,y,列轨迹,行轨迹,s)]——修补层据此定位惹祸的腿
    """
    if not legs or not ghosts:
        return []
    cols, rows = beams(legs)
    col_list, row_list = list(cols), list(rows)
    box = sweep_bbox(cols, rows)
    xmin, xmax, ymin, ymax = box
    hits = []
    for gid, gx, gy in ghosts:
        # 包围盒剪枝：列扫不到 gx 或行扫不到 gy 的鬼必安全
        if not (xmin - EPS <= gx <= xmax + EPS and ymin - EPS <= gy <= ymax + EPS):
            continue
        cov_x = [(c, t) for c, t in
                 (zip((_cover(t, gx) for t in col_list), col_list)) if c[0]]
        if not cov_x:
            continue
        cov_y = [(c, t) for c, t in
                 (zip((_cover(t, gy) for t in row_list), row_list)) if c[0]]
        if not cov_y:
            continue
        done = False
        for (ok_x, sx), ct in cov_x:
            for (ok_y, sy), rt in cov_y:
                # 时刻重合：任一 always，或两 s 相等
                if sx is None or sy is None or abs(sx - sy) < S_TOL:
                    hits.append((gid, gx, gy) if not detail else
                                (gid, gx, gy, ct, rt,
                                 sx if sx is not None else sy))
                    done = True
                    break
            if done:
                break
    return hits


def new_conflicts(existing_legs, new_legs, ghosts):
    """把 new_legs 并入 existing_legs 后【新增】的鬼点冲突（精确、增量式）。

    新冲突必然涉及至少一条新光束（新列或新行）——只枚举这些组合，
    复杂度与新腿数成正比，供解码期的逐门增量检查使用。
    """
    if not new_legs or not ghosts:
        return []
    e_cols, e_rows = beams(existing_legs)
    n_cols, n_rows = beams(new_legs)
    all_cols = sorted(set(e_cols) | set(n_cols))
    all_rows = sorted(set(e_rows) | set(n_rows))
    box = sweep_bbox(all_cols, all_rows)
    xmin, xmax, ymin, ymax = box
    out = []
    for gid, gx, gy in ghosts:
        if not (xmin - EPS <= gx <= xmax + EPS and ymin - EPS <= gy <= ymax + EPS):
            continue
        hit = False
        # 组合一：新列 × 任何行
        for t in n_cols:
            okc, sc = _cover(t, gx)
            if not okc:
                continue
            for u in all_rows:
                okr, sr = _cover(u, gy)
                if okr and (sc is None or sr is None or abs(sc - sr) < S_TOL):
                    hit = True
                    break
            if hit:
                break
        # 组合二：任何列 × 新行
        if not hit:
            for u in n_rows:
                okr, sr = _cover(u, gy)
                if not okr:
                    continue
                for t in all_cols:
                    okc, sc = _cover(t, gx)
                    if okc and (sc is None or sr is None or abs(sc - sr) < S_TOL):
                        hit = True
                        break
                if hit:
                    break
        if hit:
            out.append((gid, gx, gy))
    return out


def pair_edges(legs, ghosts, owners=None):
    """路由冲突图的鬼点边：两腿【同批】时光束组合会撞到的第三原子。

    对每对腿 (i,j)，把 {leg_i, leg_j} 的光束并集对着 ghosts 检查——命中
    即加边 (i,j)，着色器会把它们分进不同批（分批=光束不同时激活=无撞）。
    owners 给定时排除两腿自己的主人（它们在飞，不是静止鬼）。
    ghosts 通常只放第三原子的【当前相位起点】位置——比"批前批后双位置"
    稀疏得多；残余冲突由重放审计精确兜底。单腿自撞（i==j）不在此列：
    那是批化解不了的，必须由放置层保证。
    """
    edges = set()
    n = len(legs)
    for i in range(n):
        for j in range(i + 1, n):
            skip = set()
            if owners is not None:
                skip = {owners[i], owners[j]}
            if ghost_hits([legs[i], legs[j]],
                          [g for g in ghosts if g[0] not in skip]):
                edges.add((i, j))
    return edges


def hit_count(legs, ghosts) -> int:
    """便捷计数（fitness / 修补循环用）。"""
    return len(ghost_hits(legs, ghosts))


def leg_hits(leg, ghosts):
    """单腿自查：这条腿自己的列×行交叉（即自身路径）会扫到的鬼。

    菜单预过滤用——只查腿自身轨迹，不含同批其他腿的光束组合
    （那些由解码检查与修补层负责）。
    """
    d, sx, sy, tx, ty = leg
    return ghost_hits([leg], ghosts)
