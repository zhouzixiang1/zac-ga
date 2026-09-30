# 2026-09-16 英文稿对标两篇基线与DATE投稿要求

## 审阅范围

只读审阅当前英文稿、已构建PDF、两篇指定基线、冻结配置与结果来源。Overleaf远端仍为`3ed3c5fadfca681afc2915d2bfd7584d8b1f784c`，本地导出内容与之相同。没有改写论文、上传文件或运行新实验。

英文PDF SHA-256：`de887dcbb758431e930f9c2aa0f45b99aa144a230f051a0f76ca2d63cb563aa8`。审阅来源、官方模板和测量记录位于`IEEE_conference_template/build/paper_en/date-review-20260916/`。

以下三份审阅采用同一事实基础，仅侧重不同；是辅助审阅，不代表真实评审意见或录用判断。评价对象是DATE算法会议论文，非Nature期刊适配性。摘要和关键词按作者要求继续冻结。

## 对标基线：该学的是论证关系

| 论文 | 问题如何引出方法 | 证据如何收束 |
|---|---|---|
| ZAC | 分区保护空闲原子，但跨区往返增加开销；相邻层复用与布局共同减少移动 | 保真度、移动及编译代价，并明确架构和物理参数 |
| Stade等的routing-aware placement | 距离较短的放置可能迫使移动串行；兼容分组与最长轨迹进入A*放置代价 | 重排批次、重排时间和placement/routing开销 |
| GA-LK当前稿 | 完整放置方案同时决定当前执行及后续起点；从候选后配置预测多层物理损失 | 总体质量、损失分量、独立前瞻/搜索对照，以及范围和预算研究 |

前两篇并非没有前瞻或复用。ZAC已有相邻层复用及后续伙伴距离；Stade论文第四节讨论复用对未来移动的影响，第五节B将复用作为中间放置的一种选项并奖励节省的转移。GA-LK应突出完整候选的评价单位、跨过非参与层的驻留，以及从候选后状态连续展开的物理损失。该区别比“采用GA加前瞻”的模块名称更有说服力。

来源：[ZAC原文](https://vast.cs.ucla.edu/sites/default/files/publications/HPCA25_ZAC-2.pdf)、[Stade等原文](https://www.cda.cit.tum.de/files/eda/2025_iccad_routing-aware_placement_zoned_neutral_atom.pdf)。

## 审阅一：技术与证据

**总体评价。**论文对量子编译、物理设计和中性原子体系结构读者有明确相关性。两项贡献能够辨认，现有模型结果支持领域内的质量改善。更广泛的硬件实测效用不在现有证据范围内。当前主要问题是比较条件未写全，影响读者判断新意及可复现性。

**1. 应更准确区分已有复用和前瞻。**位置为`sections_en/01_introduction.tex:8`及`02_background.tex:45`。补充Stade方法将复用纳入位置选择的事实，再明确本文以完整候选执行后的配置连接多层损失。无需增加新的贡献点，也不需要重写整段引言。

**2. 实验设置缺少关键共同条件。**`sections_en/05_evaluation.tex:7–9`没有给出具体架构、主要物理参数、基线实现版本及参数。现有冻结材料可提供一个AOD、100×100存储阱、两块配对7×20纠缠阵列等条件；QMAP固定配置为3.2.0/Core3.1.0，四项参数为0.2、0.2、5、0.6。正文还应明确主结果采用各编译器的初始化，而独立对照固定起始映射。这些都是已有材料，不需要新增实验或重新调参。

来源：`ZAC_zzx/results/physical_ga_main_v1/protocol.json`、`ZAC_zzx/hardware_spec/full_architecture.json`、`fidelity-lookahead-v2/artifacts/native-ga-v1/paper-zh-v1/freeze/configs/M2.json`。ZAC原文第七节和Stade原文第六节均明确报告相关设置。

**3. 相同评分模型与相同验证筛选必须分开。**现有主表按冻结策略保留原始基线输出，不对历史基线追加GA-LK的零交点诊断筛选。本轮按当前主表的135个去重电路核验274份基线manifest，哈希、输入、架构及模型相符，确有非零历史诊断被保留：ZAC18中ZAC/Stade分别11/16、13/16；QMAP分别33/119、47/119。

这些是诊断统计，不是已经证实的物理违规数量。ZAC计数采用整条rearrangeJob起终点抽象，未展开内部LOAD/MOVE/STORE；另一基线采用归一化MOVE事件，亦受光束激活及同步运动假设影响。本轮未逐命中重建真实内部过程，不能据此断言基线不可执行、判定误报，或否定主表数值。应在比较设置交代原始输出保留口径，避免读者把统一保真度模型理解为同一严格验证器的筛选结果。详见`baseline-ghost-intersection.json`及其中代码、原始记录出处。

**4. 计算预算节尚未给出完整方法的成本。**`05_evaluation.tex:39`的21.31秒明确扣除了初始化，属于独立计时研究。正文只说完整GA初始化端到端时间另有记录，读者仍无法直接知道完整方案的开销。可从现有记录补报完整耗时及初始化占比，同时注明并行执行条件。若无同条件基线计时，就继续将其定位为预算研究，不推导速度优势。

**审阅立场。**优先补齐上述已有信息。技术描述总体可读，但完整方法的成本展示及比较筛选口径仍需加强；不据此要求无依据的大规模新实验。

## 审阅二：英文与论证连续性

**总体评价。**第三章“完整候选—当前执行—后状态预测—搜索选择”的顺序已经清楚。方法的新意与领域意义应由这一关系引领，而不是增加模块介绍。对相关领域外读者，最妨碍理解的是对象名称变动和高度压缩的实现措辞。

| 位置 | 问题 | 可采用的处理 |
|---|---|---|
| `03_method.tex:35` | “Candidates neighbor…”中的candidate从完整方案变为候选存储阱 | “Candidate return sites lie near the atom's current entangling site and initial storage site.” |
| 同段 | “Each assignment is injective; assignments are alternatives.”像实现注释 | “Each assignment maps returning atoms to distinct storage sites and completes the encoded placement plan.” |
| `05_evaluation.tex:7,17`及表I | QMAP同时指样本集与基线，出现“Against ICCAD/QMAP, QMAP yields…” | 方法统一称“the routing-aware baseline”，QMAP专指电路集；首次出现处保留实现来源和引用 |
| `05_evaluation.tex:31`及结论 | “benefit ... comes mainly from near-term interactions”把H扫参提升为物理来源归因 | “Most of the observed average improvement is obtained with one-layer look-ahead; larger bounds produce smaller changes.” |
| `05_evaluation.tex:39` | “Independent timing runs serially…”搭配生硬，过程先于结果 | 先报告扣除初始化后的21.31秒，再交代主机、固定映射、种子及两级中位数 |

引言研究问题也可进一步聚焦为：

> We address how to rank joint placement plans when each plan changes both current execution costs and the physical losses of subsequent layers.

这只是可选替换，不宜继续扩写引言。上述修改能改善可读性和论证精度，并不会改变两项贡献、数据或技术边界。对跨学科影响不作额外宣称。

## 审阅三：DATE要求与视觉交付

**总体评价。**主题与DATE的量子设计自动化方向匹配。页数及基本字体格式已具备提交基础，最优先的交付问题是匿名入口；字号和版心需按会议实际材料核对，不能单凭“看着像IEEE”判断。技术新意和意义仍取决于前述基线区别与证据，而不是版面调整。

**匿名性。**`sections_en/01_introduction.tex:11`的代码脚注直连个人GitHub账号。即使作者栏及PDF作者元数据已清空，该链接仍可识别作者。DATE要求双盲并合理避免身份暴露。建议保留代码资源，通过匿名只读入口提供；不是删除仓库。基线正式发表版可以直接放个人/机构链接，不能据此类推匿名投稿版。

**页数和字体。**现PDF为A4、双栏、正文六页、参考文献独占第七页；正文约10 pt，字体已嵌入且没有Type 3，未发现页码或版权行，也未使用压缩正文行距的baseline-stretch。官网细则明确允许表格、图注、脚注及参考文献使用8 pt，因此当前8–8.5 pt表格和图注不应一概判为违规。

**需要处理的视觉细节。**图内主要文字的最终有效字号折合TeX pt约为：图1 9.0，图2 8.7–9.2，图3 8.4–8.9，图4 8.5–8.8；算法正文9 pt。官方细则建议图内标签约10 pt，算法也没有明确的9 pt例外。优先放大图3/4主标签及算法正文，次要数学下标不必全部放大。原8.5–9 pt设计目标与这次按DATE细则核查的目标存在差别。

图3的部分基础数学字母（如q、T）实际约7.9 PDF pt，图2约8.1 PDF pt，属于主要识别标签，应一并放大。约6 pt的真正下标、竖排文字的旋转包围盒不能混入这一比较。具体测量与基线字号参照见`date-figure-format-review.json`及`figure-font-region-report.json`。

**版心存在模板差异。**当前文字横向范围约181.36 mm，官网下载ZIP模板为184.00 mm，并在源码中指定`total={184mm,239mm}`。ZIP成品PDF与当前稿的文字区纵向位置也不同。官网另附的旧format.pdf写88 mm栏宽等数值，与ZIP并不完全一致；因此这里应列为待统一的模板差异，不能断言当前已违反硬性要求。建议以实际下载的官方LaTeX模板为版式基准，再核验6＋1页。

**浮动位置。**图1–3已先引用后展示。表I在第5页右上、首次正文引用位于其下；图4在第6页左栏中部，首次正文引用也在其下。官方细则建议图表靠栏顶/底、避免出现在首次提及之前。可把指向图表的引导句提前，并调整图4浮动位置，无需重画整套图。

来源：[DATE 2027投稿要求](https://www.date-conference.com/call-for-papers)、[官方格式细则](https://www.date-conference.com/format.pdf)、[官方LaTeX模板](https://www.date-conference.com/DATE-conference-template-LaTeX.zip)。查验日期为2026-09-16。

## 综合建议与执行顺序

1. 先解决匿名仓库入口，并统一官方模板几何；保持摘要、关键词和已接受结果不变。
2. 在引言/相关工作补准确的基线区别，在IV-A补共同条件和原始基线输出保留口径。
3. 从III-D的通用路由说明、重复参数性文字中腾出篇幅，补完整编译开销的已有记录，不再扩写模块清单。
4. 统一candidate/site及QMAP名称，修正H扫参的归因措辞；调整算法字号与表I/图4引用顺序后重新构建。

三份审阅的共识是定点修订。技术侧优先可比较性，语言侧优先清晰的对象与论证，交付侧优先双盲和实际模板。已经修正的状态定义、同组参照、算法判定顺序和几何均值解释不再列为问题。

**不应从本轮审阅推导的结论：**不能将历史交点诊断当作已证实的基线物理失败，不能把不同样本对照增益相加，也不能把独立的非初始化计时当作完整GA-LK相对基线的速度结果。没有评估录用概率。

官方全文截止为2026-09-20 AoE，摘要登记截止为2026-09-13 AoE。SoftConf受登录保护的个人提交记录未在本轮核验；题目、摘要、作者列表及主/次主题是否与最终稿一致仍属于系统侧检查，不将其标成已通过。
