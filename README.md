<div align="center">

# ZAC-GA

**Research workspace for fidelity-aware compilation on zoned neutral-atom architectures**

[![Repository](https://img.shields.io/badge/repository-private-6B7280?logo=github)](https://github.com/zhouzixiang1/zac-ga)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2/ZAC_zzx)
[![C++17](https://img.shields.io/badge/C%2B%2B-17-00599C?logo=cplusplus&logoColor=white)](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2/ZAC_zzx/native)

[**Open the paper implementation →**](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2)

</div>

## Project entry points

The default branch records the consolidated ZAC research workspace. The
implementation and evidence snapshot used by the GA-LK manuscript are kept on
the dedicated [`codex/fidelity-lookahead-v2`](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2)
branch so that the paper-facing history remains auditable without merging it
into unrelated development work.

| Resource | Location |
|---|---|
| Method overview, build instructions, and evidence boundaries | [Paper-implementation README](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2#readme) |
| Python compiler integration | [`ZAC_zzx/zzx/`](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2/ZAC_zzx/zzx) |
| C++17 layer-boundary backend | [`ZAC_zzx/native/`](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2/ZAC_zzx/native) |
| Experiment and provenance pipeline | [`ZAC_zzx/experiments_v2/`](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2/ZAC_zzx/experiments_v2) |
| Verified paper artifacts | [`ZAC_zzx/results/paper_zh_v1/`](https://github.com/zhouzixiang1/zac-ga/tree/codex/fidelity-lookahead-v2/ZAC_zzx/results/paper_zh_v1) |

## Scope

GA-LK optimizes two-qubit layer boundaries by jointly considering gate-site
placement, cross-layer residency, storage-site assignment, constrained AOD
rearrangement, and a decayed finite-horizon physical cost. The manuscript
evaluation is restricted to ZAC18 and QMAP154 and compares against the original
ZAC compiler and the routing-aware ICCAD/QMAP placement method.

The repository is currently private and has no project-level license. Access,
anonymization, and licensing should be configured before external release.

