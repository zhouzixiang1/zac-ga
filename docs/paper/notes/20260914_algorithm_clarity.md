# 2026-09-14 算法1层级与逻辑修订

旧算法将候选评价、搜索和最终选择平铺为18步，难以看清控制关系。本轮保留同一求解逻辑，按三个阶段重新呈现：搜索完整候选、准入与选择、仅提交当前层。

- 用嵌套 `While/For` 明确编码搜索与完整候选评价的关系。候选先通过当前路由检查，再记录当前损失及层后配置，随后从该配置预测未来；“当前评价”和“未来预测”单独加粗。
- 搜索结束后进行有限局部调整，并重新调用前面的评价步骤。最终选择用 `If/ElsIf/Else` 区分无当前可执行候选、全部未来预测不可行及正常准入三种情况。四分之一条件、评分不升、同分排序和保留参照的规则不变。
- 提交当前层单列为最后阶段；预测配置不作为后续层的实际执行结果。
- 三阶段标题统一对齐，行号弱化，细竖线标出循环与分支范围。共用 `algorithm_style.tex` 控制两语言的9pt字号、缩进和间距，保留正常语义配色。

正文同步精简与算法重复的流程描述，英文净省59词；适应度、遗传算子、缓存预算、候选匹配、初始化和原公式均保留。原算法及正文保存在本轮轻量源码快照中。

两版正文及各自独立框架图重新构建通过：中文五页正文加一页参考文献，英文六页正文加一页参考文献。已检查算法本体、邻近图文与末页收尾，无文字越界、裁切或异常空白。摘要、关键词、图片、实验数值及数据与实施起点一致。论文测试通过，未运行新实验，未上传Overleaf。

- [算法1预览](../../../IEEE_conference_template/build/paper_zh/algorithm-clarity-20260914/algorithm_zh.png)
- [中文PDF](../../../IEEE_conference_template/build/paper_zh/paper_zh.pdf)
- [英文PDF](../../../IEEE_conference_template/build/paper_en/paper_en.pdf)
- [源码差异](../../../IEEE_conference_template/build/paper_zh/algorithm-clarity-20260914/changes.patch)
- [来源与核验记录](../../../IEEE_conference_template/build/paper_zh/algorithm-clarity-20260914/delivery-receipt.json)
