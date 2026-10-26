# Gridnote Qwen3 SQL QLoRA pilot

本实验已实际完成训练、验证集选点、基础模型/适配器成对评测及应用联调。训练后的 SQL 规划器由本机 MLX 服务运行，不是将 Ollama 原有 GGUF 配置重命名，也不是仅修改提示词。

## 结果与范围

| 指标 | 同一 MLX 基础模型 | 同一基础模型＋QLoRA |
| --- | ---: | ---: |
| 单次 SQL 契约执行正确 | 7/32（21.9%） | 22/32（68.8%） |
| SQL 执行成功 | 21/32 | 31/32 |
| QueryPlan 结构有效 | 31/32 | 31/32 |
| 直接生成子集 | 0/24 | 14/24 |
| 给定错误 SQL 的修复子集 | 7/8 | 8/8 |

改善 15 题、退化 0 题、两组均正确 7 题、两组均错误 10 题；总体增加 **46.875 个百分点**。这里检查结果字段、数值、行数与顺序，不只检查 SQL 能否运行。图表字段的有效性另行统计。

**这是有边界的小规模实验。** 测试为 8 个留出任务组合下的 32 条程序生成指令，包含相关改写；同一 schema、相同指标原语、正确的有限任务合同由程序提供，未进行独立人工复核。不能把结果写成通用 Text-to-SQL、任意自然语言理解或完整 Agent 的准确率。

![固定微调实验](../docs/figures/sft-evaluation.png)

[训练记录](../reports/sft-training.json) · [逐题成对评测](../reports/sft-evaluation.json) · [失败分析与应用联调](../reports/sft-analysis.json) · [图表 CSV](../docs/figures/sft-case-results.csv) · [绘图来源指纹](../docs/figures/sft-chart-values.json)

## 模型与实际训练

- 基座：官方 `Qwen/Qwen3-4B-MLX-4bit`，固定 revision `52a5ab34fa604bc8af6d3ce0cac0cab10b7eb495`。同源于 Qwen3-4B，但不能把 Ollama GGUF 推理结果直接当作本实验基线。
- 环境：Apple M5、16 GB 统一内存；Python 3.12、MLX-LM 0.32.2、MLX 0.32.4；完整依赖见独立 `uv.lock`。
- 4-bit QLoRA；最后 8 层，rank 8、scale 16、dropout 0；Adam、学习率 1e-4、batch 1、seed 20261026、160 步、梯度检查点。
- 训练标签是 QueryPlan JSON；只对 completion 计算损失。训练与生成均显式关闭 thinking；长度上限 1536，实际最长训练样本 992 token，没有截断。
- 实际更新 **3,670,016 个参数**；选中适配器相对初始化张量的 L2 差为 **8.24718**，不是只改变文件名或配置。
- 训练本体约 **216.97 秒**，MLX 统计峰值内存 **3.704 GB**；不含下载、数据审计与执行评测，也不是整机总内存。
- 验证使用全部 24 条样本的 completion loss；初始 0 步只诊断，选择正步数检查点中的最低损失，并预先规定评估最终步。选中第 **119 步**，验证损失约 **0.01613**，没有根据测试结果选择检查点。

权重保存在忽略的 `data/finetuning/sql-qlora-v1/best/adapters.safetensors`，适配器配置在同目录。基座张量保持冻结；训练报告记录源文件、初始/选中适配器 SHA-256 与数值更新证据。原始基座及其他依赖保留各自许可证；基座为 Apache-2.0，见官方模型页。没有上传模型权重或使用云端训练/API。

## 数据划分与标签审计

| 划分 | 指令数 | 不同任务组合 | 直接生成／修复 |
| --- | ---: | ---: | ---: |
| 训练 | 232 | 29 | 174／58 |
| 验证 | 24 | 8 | 16／8 |
| 测试 | 32 | 8 | 24／8 |

任务组合为指标类型×时间粒度×日类型过滤，不同划分之间不重叠；提问模板族和日期窗口也分开。共享单表结构和指标原语，因此不是新数据库 schema 泛化测试。

标签来自本项目有限任务编译器，是**程序生成标签**，不是人工标注或大模型人工式推理链。全部 288 个标签已通过只读 SQL 执行，并与单独解码 EIA 原始 CSV 的参考计算核对；prompt mask 前缀及长度也逐条审计。真实负荷值仅用于执行评分，不是训练标签中的数值答案。数据 SHA-256、构造器和提示模块指纹在 `datasets/manifest.json`；`common.verify_frozen` 会拒绝数据或提示被修改的运行。

## 固定评测协议

在训练和选点完成后才生成测试集模型输出。先基础模型，再装载选中 LoRA；核对适配器张量确实装入。两组使用同一基座、tokenizer、prompt、greedy temperature=0、768-token 上限、一次请求、相同 SQL 约束及独立 EIA 参考答案。没有 JSON grammar、自动重试或受信指标计算器回退。每组先用同一条验证样本热身，不共享测试 prompt cache。

这是单次、本地、固定解码设置的试验。基础模型先运行，耗时受输出长度、编译、顺序和机器状态影响；记录的中位耗时约 4.43 秒／3.11 秒只作描述，不外推为 SLA 或普遍速度提升。

首次评测在第一道基础模型题上暴露了 SQLGlot `TokenError` 未被捕获的问题。共同校验器现将未闭合引号计为查询失败，并补了回归测试；之后完整跑了 32 题×2 组。没有改训练数据、模型、检查点、提示或解码设置，也未丢弃该题。

## 保留的失败

- 7 个可执行结果漏掉了工作日／周末过滤，生成了 `WHERE 1=1`。
- 2 个结果把工作日／周末分组误写成日期分组。
- 1 个输出不是有效 QueryPlan JSON。

验证损失很低，仍不代表上述组合条件正确。现有独立数值核验会拦住这些有限任务中的错误，达到预算后停止展示。后续可补组合条件训练，但必须建立新的留出评测，不能把这批失败题训进去后继续宣称同一测试集提升。

## 复现

从仓库根目录，在 Apple Silicon Mac 上执行：

```bash
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --project training --python 3.12
training/.venv/bin/python training/download_model.py
uv run python training/prepare_data.py
HF_HUB_OFFLINE=1 training/.venv/bin/python training/audit_data.py
HF_HUB_OFFLINE=1 training/.venv/bin/python training/train.py
HF_HUB_OFFLINE=1 training/.venv/bin/python training/evaluate.py
uv run --group viz python training/visualize_results.py
```

`prepare_data.py` 需要主项目已安装（可用 `uv run python ...`）。审计及评测需要先按主 README 的命令导入 EIA-930 2024 数据。官方基座约 2.01 GiB，下载时不发送 HF token。数据、模型身份或已有运行目录不同会拒绝复用；训练和评测不会静默覆盖已完成的结果。重现实验应使用单独目录／Git checkout 并保留各自报告。固定 seed 不保证跨硬件逐位相同的权重或耗时。

## 使用选中的模型

终端 A：

```bash
HF_HUB_OFFLINE=1 training/.venv/bin/python training/serve_planner.py
```

终端 B，在本地 `.env` 设置 `ENERGY_PLANNER=mlx-sql`、`MLX_BASE_URL=http://127.0.0.1:8768`、`ENERGY_DB_PATH=data/eia-pjm.sqlite` 后：

```bash
uv run energy-agent verify-model --provider mlx --max-requests 3
uv run energy-agent serve --port 8765
```

MLX 服务串行生成 SQL，应用继续负责范围限制、只读执行、数值校验和有限纠错。模型不会运行任意 Python/Shell。联调中，1 次模型请求生成每日趋势 SQL、返回 7 行且核验通过；适配器指纹与训练检查点一致。应用“关于项目”显示固定实验图表，明确区分当前查询与历史评测。该服务是本地实验部署，未实现生产级多用户访问控制或并发调度。

这是 **MLX safetensors 适配器**，没有验证导出到 Ollama 的 GGUF 或合并部署。原有 Ollama 路径可独立选择，不能把其模板配置改动算作这次权重训练。

## 简历表述

> 为能源分析 SQL 规划组件开展 Qwen3-4B 的本地 4-bit QLoRA SFT，构建 232 条程序生成训练指令及按任务组合留出的验证／测试集；在 32 道有限契约测试中，单次 SQL 执行正确率由 21.9% 提升至 68.8%（+46.9 个百分点），保留 10 个失败案例，并接入只读执行与独立数值核验。

使用时保留“小规模、有限契约、程序生成数据”的范围，不能改写为通用模型准确率或真实业务效率收益。

官方参考：[MLX-LM LoRA/QLoRA](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md)、[官方 Qwen3 MLX 基座](https://huggingface.co/Qwen/Qwen3-4B-MLX-4bit)。本项目使用这些训练工具，新增的是业务数据与划分设计、执行审计、可核对的对照实验、失败分类及应用集成。
