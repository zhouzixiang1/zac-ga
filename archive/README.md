# 历史实现归档

本目录保存当前 GA-LK 论文实验不再直接使用的早期实现和参考工作区。归档只改变代码位置，不改变其历史内容。当前方法、实验管线和权威结果均位于仓库默认 `main` 分支。

当前入口：

- `../ZAC/`：原版 ZAC 与 Qiskit 1.2.4 运行环境；
- `../ZAC_zzx/`：GA-LK 实现、实验管线与 `paper_zh_v2` 结果；
- `../qmap-main/`：ICCAD/QMAP 源码参考；
- `../documents/`：论文材料；
- `../experiments/`：基线复现辅助脚本与证据；
- `../README.md`：当前项目概览、结果口径与复现边界。

## 已归档内容

- `FABLE/`：早期 FABLE 放置目标复现实验；
- `GA/`：早期逐层遗传放置原型；
- `ZAC_new/`：批次感知与图着色的中间原型；
- `Physics-Layer-Compiler/`：外部/参考中性原子编译器工作区；
- `Physics-Layer-Compiler.zip`：上述参考工作区的压缩副本（193 MB，本地保留，不进入 Git）；
- `neat/`：LDPC/纠错电路相关的独立 Git 仓库，保持其自身远端同步；
- `6a866ce86ea64496e2ae01a5/`：独立 IEEE LaTeX 模板 Git 仓库，保持其自身远端同步。

历史脚本若需要读取 `GA`、`FABLE`、`ZAC_new` 或 `neat`，应从本目录访问。

归档中的大体积 MP4、`tech_eval` 原始 code 轨迹和重复 zip 只在本机保留；其余可追踪源码、配置和小型结果随主仓库提交。
