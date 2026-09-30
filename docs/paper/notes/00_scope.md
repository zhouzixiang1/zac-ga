# 写作范围

- 文本类型：IEEE 双栏会议论文中文完整稿；当前进行全文表达、图表和数值呈现升级。
- 当前题目：分区中性原子量子架构的层边界联合优化与多层物理前瞻。
- 目标读者：量子编译、计算机体系结构和EDA研究者。
- 主方法：GA-LK（遗传驻留搜索与衰减多层前瞻）。
- 消融方法：GA-NL（同一类驻留搜索的无前瞻版本）。
- 实验基线：ZAC 与 ICCAD/QMAP routing-aware A*，不增加其他实验基线。
- 数据边界：只写 ZAC18 与 QMAP154；不引用 QASMBench Small/Medium/Large 新实验。
- 终稿结果状态：主实验、受控消融、敏感性与严格计时均已完成，论文交付由 `ZAC_zzx/results/paper_zh_v2/final_manifest.json` 约束；正文、表格和Fig.~6只从同目录 `paper_values.json` 及受控派生数据生成，不手工抄录数值。
- 篇幅目标：8 页正文，参考文献另页。
- 当前篇幅状态：当前构建为9页，正文占前8页，参考文献独立位于第9页；最新验收与PDF校验值见 `final_paper_qa.json`。
- 本轮文件边界：论文只引用ZAC18/QMAP154；QASMBench与Large结果不进入正文。作者、单位、地址和邮箱已填写，作者已确认无经费资助；实验及投稿元数据占位符已全部消除。
- 历史交付边界：Git历史中的 `ZAC_zzx/results/native_ga_v1` 是早期seed-0参照，已从当前 `main` 的工作树移除，不是当前论文最终manifest、数值或工作簿来源。
