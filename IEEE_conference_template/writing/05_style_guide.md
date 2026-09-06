# 中文稿学术写作与图表规范

本文件是 `paper_zh.tex` 的长期编辑准则。它把投稿规范、计算机体系结构论文的通行写法和本稿自己的证据边界分开记录。前两类决定论文应如何组织，后一类决定本稿能够主张什么。任何文字压缩、图形重绘或结果更新都不得越过 `01_research_canon.md`、`02_evidence_table.md` 和 `06_terminology_ledger.md` 所规定的事实范围。

## 1. 规范依据

### 1.1 IEEE与投稿会议的明确要求

- IEEE Conference Author Center要求引言从研究现状收束到具体问题，并说明研究动机与贡献；方法应提供足以复现的细节；结果应解释数据、承认局限且避免夸大。
- IEEE Editorial Style Manual要求图、表在正文中的首次引用按编号顺序出现，复合图的子图标号保持同一格式，正文引用、图号和图注一一对应。
- Proceedings of the IEEE建议图形在最终版面尺寸下仍可辨认，字体、线宽、符号和间距保持一致；图注应定义缩写、颜色、线型与符号，使图形能够独立阅读；优先提交矢量图。
- MICRO投稿指南要求所有图表在正文中被引用，且彩色编码在灰度打印下仍可区分。
- DAC将技术贡献、相对已有方法的可量化改进、基准证据、局限说明以及写作与组织质量列为审稿依据。

### 1.2 本稿的编辑约定

以下规则不是IEEE的逐字规定，而是结合上述规范、两个直接baseline及中文稿问题制定的统一口径：

- 正文先陈述对象、问题或观察，再在句中引用“图~X”“表~Y”；不以孤立的“如图所示”“由图可知”起段。
- 首次引用必须位于相应浮动体之前，随后只在需要建立逻辑对应时再次引用，不逐项复述图注。
- 图内使用简短英文技术标签，正文和图注使用规范中文；内部实验代号和实现动作名不进入可见论文文本。
- 每个段落只承担一个可反驳的中心命题。段首提出问题或判断，中间给出机制或证据，段末收束其适用范围或引向下一问题。

公开依据：

- IEEE Conference Author Center, *Structure Your Paper*: <https://conferences.ieeeauthorcenter.ieee.org/write-your-paper/structure-your-paper/>
- IEEE, *Editorial Style Manual for Authors*: <https://journals.ieeeauthorcenter.ieee.org/wp-content/uploads/sites/7/IEEE-Editorial-Style-Manual-for-Authors.pdf>
- Proceedings of the IEEE, *Guidelines for Figures and Tables*: <https://proceedingsoftheieee.ieee.org/resources/guidelines-for-figures-and-tables/>
- DAC, *Research Manuscript Submissions*: <https://dac.com/2026/research-manuscript-submissions>
- MICRO, *Submission Guidelines*: <https://www.microarch.org/micro59/submit/guidelines.php>
- MIT EECS Communication Lab, *Introduction*, *Methods (CS)*, *Results* and *Figure Design*: <https://mitcommlab.mit.edu/eecs/commkit/>
- Purdue OWL, *Flow in Scholarly Writing*: <https://owl.purdue.edu/owl/graduate_writing/documents/Flow-Handout.pdf>

## 2. 全文论证结构

全文按“问题—机制—证据—结论边界”组织，而不是按代码开发、参数冻结、批次运行或验收过程组织。

1. **引言：**量子计算的规模化需求 → 中性原子可重构连接的机会 → 分区架构中的跨层物理代价 → 现有编译器的决策范围 → 本文问题、方法和贡献。
2. **背景与相关工作：**器件与重排约束 → 保真度模型 → 中性原子编译方法的技术谱系 → ZAC与ICCAD/QMAP的能力边界 → 非相邻交互实例 → 本文贡献。
3. **方法：**状态和问题定义 → 联合决策变量 → 当前物理状态转换与代价 → 有限视界未来代价 → 候选准入与边界选择 → 精确枚举/遗传搜索 → 可行性、复杂度与退化关系。
4. **实验：**比较对象和统计口径 → 编译质量与高增益实例 → 多层评价与遗传搜索 → 参数选择与编译开销。
5. **结论：**回答研究问题，概括被数据支持的发现，不重复实验过程，不把未来工作写成新贡献。

章节之间必须形成问答关系：引言提出的问题由方法定义并由实验回答；方法中的每个关键机制在实验中有对应证据；实验中没有方法或主张支撑的指标不占据主要篇幅。

## 3. 段落与句子写法

### 3.1 学术段落的最小结构

- **问题句：**说明本段为何存在，例如“层边界的可行性由整组AOD轨迹决定”。
- **技术句：**给出变量、约束、公式或比较对象，不写研发经过。
- **证据句：**方法段引用定义或算法；结果段引用数值、区间、图表或文献。
- **边界句：**只在确有必要时说明适用范围，并用于过渡；不要在每段机械添加“这表明”。

结果段优先采用“验证问题—数据集与比较对象—指标—定量结果—结论边界”的顺序。单个段落通常对应一张图、一个表或一组紧密相关的统计量。正文说明读者应从图表中获得的主要判断，图注负责定义图中对象，不在两处重复逐项朗读数据。

### 3.2 降低模板化与AI痕迹

出现下列特征时必须重写：

- 以“值得注意的是”“可以看出”“进一步验证”“充分说明”“具有重要意义”代替具体主张。
- 先讲“本文做了什么步骤”，再解释为何需要这些步骤，形成实验日志式顺序。
- 连续使用“首先—其次—最后”或三个长度完全对称的句子，而没有技术因果关系。
- 用否定式元话语界定创新，如“本文不重新提出A，而是在B中耦合C”；应直接说明所求解的问题、变量和新增决策范围。
- 用实现标签代替论文概念，如内部方法编号、动作码、程序阶段名、winner或Ours。
- 把图中显而易见的所有元素逐项抄回正文，或用“随机”“示意”掩盖没有物理语义的连线和位置。
- 没有统计检验却使用“显著”，没有胜负分布却使用“普遍”“一致”“多数”。

句子应以研究对象为主语并尽早出现谓语。优先写“有限视界代价累积未来层的物理负对数保真度”，不写“为了能够进一步更好地考虑未来影响，本文设计了……”。能够由公式或数值直接表达的关系，不再添加泛化形容词。

## 4. 各节写作契约

### 4.1 引言

- 宏观背景只保留与规模、连接和误差相关的内容，不能把“量子计算前景广阔”扩写为空泛宣传。
- 技术矛盾必须逐级收束：可重构连接依赖原子输运；分区执行引入阱转移与空闲受激；局部放置不能完整反映跨非相邻层的累计代价。
- 对ZAC和ICCAD/QMAP分别写明已经解决的问题，再给出尚未覆盖的决策范围。不得用弱化baseline来制造创新。
- 贡献项采用“建立/给出/引入 + 技术对象 + 解决的具体问题”，不使用“首次”“全新”“全面”等无法核验的修饰语。

### 4.2 背景与相关工作

- 按设备模型和技术机制归类，而不是按论文发表年份逐篇罗列。
- 每组工作至少回答“解决了什么问题”和“与本文的设备语义或决策范围有何关系”。
- 引文紧随其支撑的事实；一个引文不能替代对差异的技术解释。
- 本文贡献置于本节末尾，与前述缺口一一对应。

### 4.3 方法

- 先定义状态、输入、输出和目标，再介绍求解过程。
- 每个算法步骤都能追溯到一个物理约束或目标项；不按代码调用栈介绍算法。
- 对标准技术说明如何应用及为何适合本问题，不展开教科书式定义。
- 复杂度、精确枚举与遗传搜索的切换条件应可核验；不把启发式写成最优性保证。

### 4.4 实验

- 外部基线只含ZAC与ICCAD/QMAP；主结果表只比较两个外部基线与GA-LK，并以三者共同有效的独立单元统计连续指标。GA-NL只保留在内部实验材料中，不进入当前正文或主结果表，也不得称作外部baseline。
- 表格给出精确数值，图形呈现趋势或机制；逐电路绝对值表与增幅图可互相补充，并明确交叉引用，避免逐项重复解释。
- 每项主要结论说明比较对象、样本数和聚合方式。当前正文以点估计、改善百分比与敏感性结果呈现证据；额外推断统计保留在冻结材料中。
- 运行时间作为实现代价保留一处中性陈述，不将其扩写为论文主线，也不得删除或淡化为不可见信息。

## 5. 图形规范

### 5.1 每张图的语义契约

绘图前先写一句可检验的结论，再确定支持该结论的最小图元。每张图必须同时满足：

- 有唯一图号；复合图有连续子图号，正文和图注使用相同编号。
- 对应一个可复述的器件状态、门层转换、候选比较或实验数据，不画无来源的装饰性箭头。
- 原子身份在相邻快照中守恒；门对、存储位、AOD轨迹、冲突边和状态变量均有明确含义。
- 图注定义各子图、颜色、实虚线、符号和必要缩写，使读者不依赖正文也能还原场景。
- 颜色不是唯一编码；打印为灰度后仍能依靠形状、线型、填充和标签区分对象。

本稿六张核心图的固定语义如下：

| 图号 | 真实对象 | 图中必须保留的对应关系 |
|---|---|---|
| Fig. 1 | 分区阵列与三类AOD并行约束 | $q_0,q_1$执行CZ，$q_2$接受局域Raman旋转，$q_3$跨区输运；非交叉、保持关系、非目标交点三种约束分别编号 |
| Fig. 2 | 非相邻CZ交互的驻留与存储位选择 | $q$在$L_\ell$与$p$成对、在$L_{\ell+3}$与$r$成对；中间两层的实际门对可追踪。$q$位于直达通道上的$s_1$时，静态占用使$u\to u'$单轨迹不可行，拆分并行批次不能修复；重选通道外的$s_2$或加入中转后方可形成可行路径 |
| Fig. 3 | 分区中性原子的端到端编译流程 | 输入与调度使用同一门集；$\pi_0,\pi_\ell,\pi'_\ell$中的逻辑原子身份守恒；重排批次和ZAIR指令对应实际层序 |
| Fig. 4 | 一个层边界的联合候选及求解 | 两个门位基因与目标门对应；$\mathcal D_\ell=\{q_3\}$时第一驻留基因对应$q_3$；枚举/遗传切换条件、不同候选和轨迹约束均有定义 |
| Fig. 5 | 候选级物理评价与有限视界比较 | 冲突图延续Fig. 3的移动轨迹；$B_1/B_2$来自图中的兼容关系；候选A/B从各自后状态展开未来层 |
| Fig. 6 | GA-LK相对ZAC和ICCAD/QMAP的总体与逐电路改善 | 采用双面板分组柱状图；面板(a)使用全部共同有效独立单元，面板(b)展示四个高增益实例。两轴均为$100(F_G/F_B-1)$，范围统一为0–45%，从零起画；两种基线由颜色、填充与图例区分 |

### 5.2 绘制与排版

- 核心示意图使用TikZ；统计图由LaTeX/PGFPlots读取受控数据文件生成。不得把现成截图或位图当作论文主图。
- 独立插入的PDF必须为单页矢量图、裁去多余白边、嵌入全部字体且不含raster对象。
- 在论文最终缩放尺寸下检查字体、箭头、线宽和留白。图内字号以正文可读为准，不通过整体缩小掩盖重叠。
- 图形直接按最终栏宽设计：单栏约88.9 mm，双栏约182 mm。关键文字以8--10 pt为目标，承载结论的标注不得低于7 pt；若容不下，应删去重复解释或重组面板，而不是继续缩小字号。
- 最终尺寸下的辅助线宜不细于0.5 pt，区域边界不细于0.7 pt，关键轨迹宜为0.8--1.0 pt。数值是本稿的内部可读性下限，不是IEEE的强制线宽条款。
- 全文的纠缠区、存储区、SLM/AOD原子、门位、可行/不可行路径采用同一形状、配色和线型。
- 标题、公式和注释放入预留区域，不覆盖轨迹、原子或边框；所有箭头终点必须落在所指对象上。
- 坐标轴写明量、单位和优化方向；截断坐标或超界点必须显式标记，并保留用于统计的原始数据。

## 6. 表格规范

- 表格用于精确比较，图形用于趋势和分布。表头给出单位及“越大/越小越好”的方向。
- 单栏表占满`\columnwidth`，双栏表占满`\textwidth`；统一使用`\PaperTableSetup`、`tabularx`或`tabular*`和`booktabs`，不以`\resizebox`拉伸表格。仅在密集表中局部调整`\tabcolsep`，不得以缩小至不可读字号换取塞入。
- 全文不使用竖线、底色卡片或不同的表题样式；表题置于表上方，表注紧随底线，字段顺序按“对象—规模—基线—本文—相对量”组织。
- baseline与GA-LK并列，样本总数、共同有效样本数、聚合方式和缺失口径在表注中说明。
- 粗体只表示所列方法中的最佳点估计；若统计意义另有条件，使用独立符号并在表注中定义。
- QMAP154的总体几何均值、逐电路统计和高增益样例采用明确区分的统计口径，避免用单一汇总量替代逐电路证据。
- 不隐藏相反指标：保真度、阱转移、空闲受激、重排时延和编译时间各按其物理含义报告。

## 7. 固定证据边界

- 正文实验仅使用ZAC18和QMAP154；不引入QASMBench或Large。
- 完整方法为GA-LK，GA-NL为不读取有序未来层的独立配置内部对照；baseline仅为ZAC和ICCAD/QMAP。
- $H_{\max}=8$/$H_{\max}=0$对照共享$(\alpha,\rho,\epsilon)=(0.5,0.7,0.05)$及其余设置，只支持受控集合内视界配置的整体比较：后者令可见未来层集合及相应驻留保护集合为空，同时保留当前边界的物理约束与候选构造规则，不写成单个未来代价项的独立贡献。遗传搜索的作用只由同目标遗传/贪心对照支持。
- QMAP154中的个别电路具有较大的模型保真度增益；相关结论不扩写为所有电路上的一致提升。
- GA-LK的编译时间明显高于baseline，当前稿以一处正文中性陈述如实呈现。
- 模型保真度来自ZAC参数化模型，不写成硬件实测保真度。

## 8. 长期验收流程

每轮修改按以下六道门执行；任一道未通过，不以“文字已润色”视为完成。

1. **事实门：**核对研究规范、结果宏和引用，确保每个主张有代码、数据、公式或文献支撑。
2. **论证门：**检查每节是否完成其问题—机制—证据关系，删除研发时间线和重复解释。
3. **语言门：**搜索禁用术语、元话语、空泛形容词和模板化过渡，逐段改为具体技术主语。
4. **图表门：**核对编号、真实场景、身份守恒、图注自洽、灰度可辨、单位和统计口径。
5. **排版门：**执行XeLaTeX与自动验证，逐页检查重叠、越界、空白、浮动体距离和参考文献页。
6. **外部读者门：**要求不参与实现的读者仅凭摘要、图表和结论复述研究问题、方法增量、主要证据及边界；无法复述之处必须重写。

当前固定回归命令为：

```bash
python3 verify_paper_zh.py --compile --expected-pages 9
```

修改完成后还须运行 `git diff --check`，再根据用户要求提交与推送；不得以清理工作树为由覆盖未提交的论文修改。

数值呈现另由 `writing/audit_numerical_presentation.py` 只读核对冻结结果、基准规模、受控参数与PDF中的改善百分比；记录不得由格式验收替代。最终PDF校验值统一查看 `writing/final_paper_qa.json`。

## 9. IEEE官方规范与低模板化写作检查

以下规则来自IEEE Author Center与IEEE Editorial Style Manual；具体投稿会议的页数、匿名与模板要求优先。

- 标题保持具体、简洁和描述性，不用“novel”“new”等词代替贡献论证。摘要保持单段、自包含，不放引用、脚注、缩略语和数学公式；问题、方法、证据与边界各承担明确功能。关键词控制在3--5个。
- 缩略语在正文第一次出现时给出全称，即使其曾在其他位置解释；标题中避免非必要缩略语。
- 引言由领域问题逐步收束到本文可检验的问题。段落首句应承载技术主张，随后给证据或机制；删除“值得注意的是”“可以看出”等不增加信息的过渡。
- 方法披露架构假设、变量、物理模型、预算、停止条件、随机性和失败处理。标准算法只说明其在本文中的接口与作用，不复述教科书或代码调用栈。
- 结果按“比较对象与统计单位—数值与不确定性—解释与适用边界”组织。完成数、失败、超时和缺失口径不可省略；图表中的数值不在正文逐项重抄。
- 每张图和表都必须在正文中被引用，并按编号顺序首次出现；浮动体放在首次引用之后并尽量靠近。英文终稿使用`Fig. 1`、`Fig. 1(a)`和罗马数字`TABLE I`；中文工作稿统一使用“图”“表”，不得在段首直接以交叉引用代替技术主语。
- 图注应能独立说明对象、子图、符号、聚合量和误差含义。矢量图优先使用PDF/EPS，嵌入字体；线型、点型与填充须保证灰度打印可辨，不能只依赖颜色。
- 可复现性要求不止于仓库链接：固定提交、依赖、运行命令、随机种子、预算、预计时间、输出路径及论文图表映射均应可查。
- 若最终投稿稿件含生成式AI直接生成的文字、图或代码，按IEEE投稿政策在致谢中披露系统、涉及部分和使用程度；参考文献与技术事实必须由作者逐项复核。

官方来源：

- [IEEE Conference Author Center: Structure Your Paper](https://conferences.ieeeauthorcenter.ieee.org/write-your-paper/structure-your-paper/)
- [IEEE Editorial Style Manual for Authors](https://journals.ieeeauthorcenter.ieee.org/wp-content/uploads/sites/7/IEEE-Editorial-Style-Manual-for-Authors.pdf)
- [IEEE Conference Author Center: Improve Your Graphics](https://conferences.ieeeauthorcenter.ieee.org/write-your-paper/improve-your-graphics/)
- [IEEE Author Center: Resolution and Size](https://journals.ieeeauthorcenter.ieee.org/create-your-ieee-journal-article/create-graphics-for-your-article/resolution-and-size/)
- [IEEE Author Center: Research Reproducibility](https://journals.ieeeauthorcenter.ieee.org/create-your-ieee-journal-article/research-reproducibility/)
- [IEEE Conference Author Center: Submission Policies](https://conferences.ieeeauthorcenter.ieee.org/author-ethics/guidelines-and-policies/submission-policies/)
- [University of Manchester: Academic Phrasebank](https://www.phrasebank.manchester.ac.uk/about-academic-phrasebank/)
- [MIT Communication Lab: Journal Article](https://mitcommlab.mit.edu/cee/commkit/journal-article/)

## 10. 科学图形工程基线

联网规范调研不能停留在“使用矢量图”。每张图在进入正文前还须满足以下工程检查：

1. **先写结论，再画图。** 为图写一句可证伪陈述；图中每个面板和图元都必须服务该陈述。流程图分别标出输入、状态、决策、约束和输出，结果图分别标出比较对象、统计单位、零基线和完整样本范围。
2. **按最终尺寸绘制。** TikZ和PGFPlots的坐标与文字密度按目标栏宽设计；不得依赖`\resizebox`把任意大画布压缩到不可读。若图内关键文字低于7 pt，优先拆分面板、缩短标签或把解释移入图注。
3. **采用冗余编码。** 方法、状态和有效性同时由颜色与线型、点型、填充或直接标签区分；灰度渲染后仍应得到相同判断。浅色区域中的正文标注使用黑色或高对比深色。
4. **只保留信息图元。** 禁用渐变、阴影、3D柱、圆角卡片、拟物图标和无物理意义的曲线。辅助网格仅在帮助定量读数时保留，视觉层级由线宽、明暗和留白建立。
5. **保持物理身份。** 状态快照标明层或状态；同一原子跨面板保持编号；输运箭头从真实源阱落到真实目标阱；双比特门显示实际门对；不可行候选标出相交、次序破坏或非目标交点占用等具体约束。
6. **让图注自包含。** 第一分句概括整图回答的问题，随后按子图顺序解释对象和编码，并定义样本数、聚合量、误差或置信区间。图注不复述正文中的方法步骤。
7. **表格保持可比较。** 使用原生`tabularx`或`tabular*`和`booktabs`，单位与优化方向置于表头，小数按位对齐；粗体只表示同一可比条件下的最佳点估计。均值、中位数、几何均值、IQR、失败和超时口径写入表注。

图形提交前执行：

```bash
pdfinfo figure.pdf
pdffonts figure.pdf
pdfimages -list figure.pdf
pdftoppm -png -r 300 figure.pdf tmp/figure
```

并在300 dpi彩色图、灰度图和论文整页三个尺度上检查：文字可读、颜色冗余、边界与标签无交叠、图注可独立解释结论。相关依据包括[IEEE图形制作指南](https://journals.ieeeauthorcenter.ieee.org/create-your-ieee-journal-article/create-graphics-for-your-article/)、[IEEE图形尺寸与分辨率](https://journals.ieeeauthorcenter.ieee.org/create-your-ieee-journal-article/create-graphics-for-your-article/resolution-and-size/)、[Proceedings of the IEEE图表指南](https://proceedingsoftheieee.ieee.org/resources/guidelines-for-figures-and-tables/)、[ACM DIS无障碍图表指南](https://dis.acm.org/2023/creating-accessible-figures-and-tables/)和[MIT Figure Design](https://mitcommlab.mit.edu/aeroastro/commkit/figure-design/)。

## 11. 低模板化逐段审校

“像人写”不以规避检测器为目标，而以技术判断可追踪为标准。每段审校时依次回答：主语是什么；作出了什么判断；判断由哪个公式、图表、实验或文献支持；结论止于哪里。无法回答其中任一项的句子应删除、拆分或补证据。

- 用真实技术对象作主语，例如“物理检查器拒绝相交轨迹”，避免连续使用“本文”“该方法”“这一机制”。
- 让动作落在动词上，避免“进行……处理”“实现对……的考虑”等名词化空壳；主语与谓语尽量相邻。
- 句首承接已知对象，句末放新增判断；每句只承载一个主要关系，每段只承担一个论证功能。
- “因此”“导致”“归因于”只用于机制或对照实验足以支持的因果关系；否则使用“对应”“伴随”“与……一致”等非因果表达。
- “显著”只表示统计显著或正文已明确给出足以支持该词的量级；“有效”“重要”“全面”等修饰语必须能立即回答“相对谁、在哪个指标、什么范围”。
- 删除目录式叙述、实现过程、研发时间线和对称排比。图表引用嵌入证据句，不以“如图所示”“由表可知”独立起段。
- 保留术语稳定性，不通过同义替换制造所谓自然感；方法、数据集、状态和指标在全文各使用一个规范名称。

该审校法综合了[Stanford计算机论文写作指南](https://cs.stanford.edu/people/widom/paper-writing.html)、[MIT结果段指南](https://mitcommlab.mit.edu/meche/commkit/journal-article-results/)、[George Mason简洁写作指南](https://writingcenter.gmu.edu/writing-resources/general-writing-practices/writing-concisely)和[Academic Phrasebank的使用边界](https://www.phrasebank.manchester.ac.uk/about-academic-phrasebank/)。短语库只用于识别修辞功能，不得拼接成套句型或替代作者的技术判断。
