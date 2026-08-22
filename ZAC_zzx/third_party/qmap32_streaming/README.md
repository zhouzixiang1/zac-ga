# QMAP 3.2 routing-aware Large 流式冻结包

此目录把正式 Large 实验的 M2 固定在 ICCAD 论文对应的
`mqt-qmap v3.2.0` 上。补丁只增加有界内存的逐层接口、SQLite 调度层读取器、
原生输出 CLI 和等价性测试；M2 仍使用原版 routing-aware A*，不允许
routing-agnostic fallback。

## 冻结基线与完整性

- 上游：`https://github.com/munich-quantum-toolkit/qmap.git`
- tag：`v3.2.0`
- base commit：`745d56b260c06708e1211772f0ed174b7a6c3437`
- 补丁：`mqt-qmap-v3.2.0-streaming.patch`
- 补丁 SHA256：`a8fdb8eccf6face830cccf468b98f474de912652a62c0cf958e611054239a0c8`
- 补丁范围：24 个文件，2673 行新增、187 行删除；其中 5 个是新增文件。
- 冻结配置：`routing_aware_paper.json`
- 配置 SHA256：`b0bde4c5f37ebe174d226a40b6b1e6a9598222829409ec5e25de46486af96378`
- binary freeze revision：`1`
- 正式 CLI 相对路径：`build-transition-tests/src/na/zoned/mqt-qmap-na-zoned-stream`
- 正式 CLI SHA256：`c6b716e441cab1574c7f890d23652ce516142c9b24aa06d709d611520867b74d`
- 正式 CLI 大小：`909528` bytes（当前冻结产物为 macOS arm64 Mach-O）。

机器可读的 base、哈希和 24 个路径账本在 `freeze_manifest.json`。先执行：

```bash
python3 ZAC_zzx/third_party/qmap32_streaming/verify_frozen_patch.py
```

manifest 还冻结了补丁应用后 24 个文件各自的 SHA256，以及当前正式构建 CLI
的相对路径、SHA256 和字节数。正式 runner 必须以 `--patched-source-tree`
等价检查验证 CLI 所在源码树和二进制，单独验证补丁文本不足以形成正式
provenance：

```bash
python3 ZAC_zzx/third_party/qmap32_streaming/verify_frozen_patch.py \
  --patched-source-tree /path/to/patched/mqt-qmap-v3.2.0
```

也可将一个未修改的 v3.2.0 源码树交给检查器，同时验证 base SHA、工作树清洁
和 `git apply --unidiff-zero --check`。补丁使用零上下文 unified diff，避免把
patch 文件自身的空上下文前缀误记为行尾空格：

```bash
python3 ZAC_zzx/third_party/qmap32_streaming/verify_frozen_patch.py \
  --source-tree /path/to/clean/mqt-qmap-v3.2.0
```

## 应用、构建与测试

以下命令从干净基线重建 M2 Large 编译器：

```bash
git clone https://github.com/munich-quantum-toolkit/qmap.git /path/to/mqt-qmap-v3.2-stream
git -C /path/to/mqt-qmap-v3.2-stream checkout --detach \
  745d56b260c06708e1211772f0ed174b7a6c3437
git -C /path/to/mqt-qmap-v3.2-stream apply --unidiff-zero --check \
  /path/to/zac/ZAC_zzx/third_party/qmap32_streaming/mqt-qmap-v3.2.0-streaming.patch
git -C /path/to/mqt-qmap-v3.2-stream apply --unidiff-zero \
  /path/to/zac/ZAC_zzx/third_party/qmap32_streaming/mqt-qmap-v3.2.0-streaming.patch

cmake -S /path/to/mqt-qmap-v3.2-stream \
  -B /path/to/mqt-qmap-v3.2-stream/build-stream \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_MQT_QMAP_TESTS=ON \
  -DBUILD_MQT_QMAP_BINDINGS=OFF
cmake --build /path/to/mqt-qmap-v3.2-stream/build-stream \
  --target mqt-qmap-na-zoned-stream mqt-qmap-na-zoned-test -j2
/path/to/mqt-qmap-v3.2-stream/build-stream/test/na/zoned/mqt-qmap-na-zoned-test
ctest --test-dir /path/to/mqt-qmap-v3.2-stream/build-stream \
  -R mqt-qmap-na-zoned-stream-help --output-on-failure
```

上面的重建产物只能用于验证或下一版冻结，不能替代 revision 1 的正式 CLI。
任何重建（包括同一源码和参数）都必须递增 `freeze_revision`，重新记录二进制
相对路径、SHA256、大小并重跑全部冻结验证；不得沿用 revision 1 的正式资格。

补丁测试覆盖增量 ASAP、reuse、routing-aware A*、independent-set routing、
code generation、完整 compiler 的 batch/stream 等价性，以及 SQLite provider
和 CLI。冻结时原 clone 的 zoned 测试为 155/155 通过，CLI help 测试也通过。

## 生成统一输入

CLI 读取 `qmap-schedule-store-v1` SQLite，而不是再次 transpile QASM。正式
Large 输入应先由 `streaming.qmap_schedule_sqlite` 从同一 canonical gate store
生成。当前 `full_architecture.json` 的 QMAP 调度容量为 140（纠缠区首个 SLM
的 `7 x 20` sites），与 QMAP 3.2 `ASAPScheduler` 的容量计算一致：

```bash
PYTHONPATH=/path/to/zac/ZAC_zzx python3 - <<'PY'
from streaming.qmap_schedule_sqlite import build_qmap_schedule_store

build_qmap_schedule_store(
    "/path/to/canonical-layer-store.sqlite",
    "/path/to/qmap-schedule.sqlite",
    max_two_qubit_gates_per_layer=140,
)
PY
```

ZAC 架构 JSON 必须转换为 QMAP 的字段名；不要手工改 JSON：

```bash
python3 /path/to/zac/experiments/spec_convert.py \
  /path/to/zac/ZAC_zzx/hardware_spec/full_architecture.json \
  /path/to/run/full_architecture.qmap.json
```

该转换会处理 `rydberg -> rydberg_gate`、`1qGate -> single_qubit_gate`，以及
ZAC 历史拼写 `site_seperation` / `dimenstion`。正式运行应对源架构、转换后架构、
SQLite schedule 和配置分别记录 SHA256。

## 检查与正式编译

先只读检查 metadata 和前若干 scheduled items：

```bash
/path/to/mqt-qmap-v3.2-stream/build-stream/src/na/zoned/mqt-qmap-na-zoned-stream \
  --inspect-schedule /path/to/qmap-schedule.sqlite 4
```

正式编译命令为：

```bash
/path/to/mqt-qmap-v3.2-stream/build-stream/src/na/zoned/mqt-qmap-na-zoned-stream \
  /path/to/qmap-schedule.sqlite \
  /path/to/run/full_architecture.qmap.json \
  /path/to/zac/ZAC_zzx/third_party/qmap32_streaming/routing_aware_paper.json \
  /path/to/run/native.na \
  /path/to/run/transitions.jsonl
```

两个输出路径必须互不相同且事先不存在。CLI 逐层写出原生 NA 指令和 placement、
reuse、routing metadata；成功后才原子发布输出。`--inspect-schedule` 的数字参数
是要读取的 scheduled-item 数量，不能超过数据库 metadata 中的
`scheduled_items`。

冻结配置对应论文参数：`deepeningValue=0.2`、`lookaheadFactor=0.2`、
`reuseLevel=5.0`、`deepeningFactor=0.6`、`maxNodes=50000000`，即计划中固定的
`alpha=0.2, beta=0.2, gamma=5, delta=0.6`。CLI 会在启动时拒绝配置漂移。
