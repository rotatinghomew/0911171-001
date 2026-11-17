# 实验观察分析

容器环境固定在仓库 Dockerfile 的 Python 3.11.14 镜像摘要，数值分析依赖与传递依赖在 requirements.txt 中精确锁定。执行 `docker build -t local-analytics .` 后，`docker run --rm --network none local-analytics` 运行内置例子；`docker run --rm --network none local-analytics python -m unittest discover -s tests -v` 运行测试。构建时安装依赖，运行时无需联网；非容器环境使用 Python 3.11，并先执行 `python -m pip install -r requirements.txt`。

这是供内容团队复盘线上实验的本地分析服务原型。服务将分组、转化事件与报告计算分开，演示数据均为合成记录。

运行 `python -m unittest discover -s tests -v` 检查项目；从仓库根目录运行 `python -m experiment.demo` 查看报告。

实验分组由稳定标识决定，事件按唯一事件编号保存，报告目前按给定截止时间统计已收到的转化。示例不是总体效果结论。

`experiment.cohorts.Cohort` 登记入组时刻和业务分群，同一用户重复登记不改变分组，冲突的入组信息会被拒绝。`observed_report` 只纳入截止点前入组的人群，并调用现有事件台账和转化汇总。当前截止点是事件发生时间，台账保留每个事件编号的最新内容；当前报告是已观测的描述统计，不提供显著性判断。时间字段为同一时钟上的整数秒。
