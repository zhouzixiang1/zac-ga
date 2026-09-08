# 新增32电路：独立扩展

预定 32 项；完整三种子 30 项。

分析单位为电路：先在电路内平均种子的 log fidelity，再跨完整电路计算几何保真度；批次为算术均值之比。
不完整项保留；其下表比较仅使用各臂共同有效种子，标为描述性，不进入主汇总。串行先导和并行扩展的墙钟时间不合并。

- h2_vs_h0: fidelity +0.06426764%; batch reduction +0.06775986%; fidelity W/T/L {'wins': 11, 'ties': 17, 'losses': 2}; batch W/T/L {'wins': 6, 'ties': 21, 'losses': 3}.
- h0_vs_sa_reference: fidelity -0.02527228%; batch reduction -0.03795993%; fidelity W/T/L {'wins': 7, 'ties': 15, 'losses': 8}; batch W/T/L {'wins': 4, 'ties': 18, 'losses': 8}.
- h2_vs_sa_reference: fidelity +0.03897911%; batch reduction +0.02982566%; fidelity W/T/L {'wins': 12, 'ties': 13, 'losses': 5}; batch W/T/L {'wins': 4, 'ties': 21, 'losses': 5}.

| Circuit | Source | Qubits | Seeds/status | H2/H0 fidelity gain % | H2/H0 batch reduction % | H2/SA fidelity gain % |
|---|---|---:|---|---:|---:|---:|
| ghz_n78_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 78 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| seca_n11_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 11 | 3/3 complete | +0.452153 | -0.421941 | +0.690047 |
| ghz_n40_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 40 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| knn_n31_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 31 | 3/3 complete | +0.024209 | +0.251889 | -0.139869 |
| qft_n29_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 29 | 2/3 descriptive; success, success, timeout | +0.000000 | +0.000000 | +0.000000 |
| bv_n30_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 30 | 3/3 complete | +0.000000 | +0.000000 | +0.040545 |
| ising_n98_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 98 | 0/3 descriptive; timeout, timeout, timeout | — | — | — |
| cat_n35_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 35 | 3/3 complete | +0.005485 | +0.000000 | +0.005485 |
| multiply_n13_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 13 | 3/3 complete | +0.000000 | +0.000000 | +0.028391 |
| bv_n70_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 70 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| swap_test_n25_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 25 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| cat_n22_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 22 | 3/3 complete | -0.002988 | +0.000000 | -0.022760 |
| bv_n19_transpiled | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 19 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| squar5_261 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 13 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| sf_276 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 6 | 3/3 complete | +0.070228 | +0.151286 | +0.045396 |
| cm152a_212 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 12 | 3/3 complete | +0.000000 | +0.000000 | +0.746823 |
| sqrt8_260 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 12 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| con1_216 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 9 | 3/3 complete | +0.000000 | +0.000000 | +0.295572 |
| z4_268 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 11 | 3/3 complete | +0.217048 | -0.057002 | +0.000532 |
| rd53_130 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 7 | 3/3 complete | +0.000000 | +0.000000 | -0.482613 |
| rd53_251 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 8 | 3/3 complete | +0.817106 | +0.678119 | -0.001254 |
| rd73_252 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 10 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| 4gt11_84 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| 4gt5_76 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | +0.129387 | +0.540541 | -0.128616 |
| majority_239 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 7 | 3/3 complete | +0.193420 | +0.284091 | +0.000000 |
| qe_qft_4 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | +0.031022 | +13.333333 | +0.031022 |
| one-two-three-v0_98 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| rd32_270 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | +0.000696 | +0.000000 | +0.000696 |
| 4mod7-v0_94 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | -0.051359 | -0.366300 | +0.000000 |
| ground_state_estimation_10 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 13 | 3/3 complete | +0.000000 | +0.000000 | +0.000000 |
| alu-bdd_288 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 7 | 3/3 complete | +0.000000 | +0.000000 | +0.020818 |
| 4gt10-v1_81 | [parallel_extension](/Users/zhouzixiang/Desktop/zac/ZAC_zzx/results/initial_lookahead_v1/dynamic39-missing32-h0-h2-sa-k4-rho07-b32-seed012-p3-v1/protocol.json) | 5 | 3/3 complete | +0.045903 | +0.000000 | +0.045903 |
