# AI造梦机 · RAG 模型评测

> 更新：2026-10-06  
> 来源：`docs/evidence/RAG/` 历史 4 份评测 JSON，以及同日多模型对比批次 `rag-eval-model-*-20261006*.json`；包装脚本 `/tmp/rag_embed_compare/run_compare.py`（**未改项目源码**）。  
> 范围说明：在保持切分 380/60、Top-5、上下文预算 12000 字不变的前提下，对比 Embedding 模型；阈值先固定 0.48，再**仅在开发集**上校准，留出集只做验证。

## 1. 候选与可达性

| 候选 | FastEmbed 支持 | 结果 |
| --- | :---: | --- |
| `BAAI/bge-small-zh-v1.5`（基线，512 维） | 是 | 完成开发集 / 留出集评测 |
| `BAAI/bge-base-zh-v1.5` | **否** | `TextEmbedding` 直接报 `ValueError: Model ... is not supported`；见 `rag-eval-model-bge-base-zh-v1.5-UNSUPPORTED-20261006.json` |
| `BAAI/bge-m3` | **否** | 同上；见 `rag-eval-model-bge-m3-UNSUPPORTED-20261006.json` |
| `jinaai/jina-embeddings-v2-base-zh`（768 维） | 是 | 作为 FastEmbed 当前可用的中文/多语言替代候选完成评测（**注明：不是 bge-base / bge-m3 本身**） |

运行时：fastembed 0.7.4、onnxruntime、本机 CPU（macOS arm64）；向量库 qdrant-client 本地磁盘模式；无付费云端 Embedding API。

## 2. 阈值 0.48 固定对比（未调参）

| 模型 | 集合 | 开发集 Recall@5 | 开发集约束覆盖 | 开发集关键通过率 | 留出集 Recall@5 | 留出集约束覆盖 | 留出集关键通过率 | H01 | H07 | H08 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | :---: | :---: |
| bge-small-zh-v1.5 | 开发 / 留出 | **1.00** | 1.00 | 1.00 | **0.80** | 1.00 | 0.33 | 未过 | 未过 | 未过 |
| jina-v2-base-zh | 开发 / 留出 | 0.77 | 1.00 | 1.00 | 0.70 | 1.00 | 0.33 | 未过 | 未过 | 未过 |

说明：

- 基线在阈值 0.48 上**可复现**此前结论：开发集全过，留出集 Recall@5 = 0.80，失败用例仍为 H01 / H07 / H08。
- jina 在同一阈值下开发集漏召回 R01、R06、R07（分数整体偏低），留出集额外失败 H05。

证据：`rag-eval-model-bge-small-zh-v1.5-*-t048-20261006.json`、`rag-eval-model-jina-embeddings-v2-base-zh-*-t048-20261006.json`。

## 3. 开发集校准后再测

校准规则：只用开发集 `raw_score_diagnostic`，在 0.20–0.80 步长 0.01 扫描；优先保证无答案用例（R12/R13/R14）清空普通命中，再最大化有答案宏平均 Recall@5；**不看留出集**。

| 模型 | 校准阈值 | 开发集 Recall@5 | 开发集约束 / 关键 | 留出集 Recall@5 | 留出集约束 / 关键 | H01 | H07 | H08 |
| --- | ---: | ---: | --- | ---: | --- | :---: | :---: | :---: |
| bge-small-zh-v1.5 | 0.49 | **1.00** | 1.00 / 1.00 | **0.80** | 1.00 / 0.33 | 未过 | 未过 | 未过 |
| jina-v2-base-zh | 0.38 | **1.00** | 1.00 / 1.00 | **0.80** | 1.00 / 0.33 | 未过 | 未过 | 未过 |

观察：

1. 校准后两者开发集都能到 Recall@5 = 1.00；留出集都是 0.80，**未过 0.85 门槛**，且同样卡在 H01（意译漏召回）与 H07 / H08（同域无答案强召回）。
2. jina 需要把阈值降到 0.38 才能在开发集追平基线；分数尺度与 bge-small 不同，**不能共用 0.48**。
3. 换 Embedding **没有消除** H01 / H07 / H08，与既有判断一致：同域无答案不能靠相似度阈值解决，意译漏召回需混合检索或重排等后续手段。

证据：`*-dev-calibrated-t049-20261006.json`、`*-holdout-calibrated-t049-20261006.json`、`*-dev-calibrated-t038-20261006.json`、`*-holdout-calibrated-t038-20261006.json`。

## 4. 性能与体积

| 模型 | ONNX 权重体积 | 首次下载+加载 | 缓存后加载 | 评测内模型初始化（代表值） | 首次建索引（开发集 t048） | 检索 p50 / p95（开发集校准后） | 检索 p50 / p95（留出集校准后） |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| bge-small-zh-v1.5 | ≈ 90 MB（`model_optimized.onnx` 94,781,076 B） | 已在项目缓存，本轮未重新下载 | ≈ 105 ms | ≈ 30–37 ms | ≈ 381 ms | 2.70 / 5.02 ms | 3.77 / 5.55 ms |
| jina-v2-base-zh | ≈ 611 MB（`onnx/model.onnx` 641,212,851 B；目录标称 0.64 GB） | ≈ 39.3 s（含下载） | ≈ 380 ms | ≈ 216–237 ms | ≈ 672 ms | 14.40 / 17.53 ms | 14.49 / 17.00 ms |

jina 首次下载写入 `/tmp/rag_embed_compare_models`（未写入用户真实知识库 `data/knowledge` 下的资料与向量）；基线继续使用既有 `data/knowledge/models` 只读缓存。评测一律走 `evaluate_rag` 的临时目录，`real_user_data_written: false`。

## 5. 结论（基于本轮数据）

1. **在 FastEmbed 可达的候选里，基线 `bge-small-zh-v1.5` 仍更合适**：校准后留出集 Recall@5 与 jina 持平（均为 0.80），但体积约为 jina 的 1/7，检索约快 5 倍，且无需改集合维度与重建成本更低。
2. **`bge-base-zh-v1.5`、`bge-m3` 本轮无法在 FastEmbed 路径下实测**；若要坚持这两款，需换运行时（例如 sentence-transformers / 自建 ONNX），那是另一条工程方案，不在本次对比内。
3. 多模型对比**没有**带来留出集召回提升；后续仍应按原计划做混合检索 / 重排与答案充分性，而不是指望换更大 Embedding。

## 6. 历史单模型批次（保留）

此前仅跑 bge-small 的批次结论仍然有效，可与本轮基线复跑对照：

| 批次 | 阈值 | 有答案用例 | Recall@5 | 门槛 0.85 |
| --- | ---: | ---: | ---: | :---: |
| 锚点试跑 | 0.55 | 2 | 0.50 | 未过 |
| 开发集初跑 | 0.55 | 11 | 0.8182 | 未过 |
| 开发集校准后 | 0.48 | 11 | 1.00 | 过（调参后） |
| 独立留出集 | 0.48 | 5 | 0.80 | 未过 |

本轮基线在 0.48 上复现：开发集 1.00、留出集 0.80、H01/H07/H08 仍失败。

## 附：混合检索 / 重排（2026-10-06）

Embedding 对比之外，另做了 BM25+RRF 混合检索与 `BAAI/bge-reranker-base` 重排离线评测（未改生产代码）。留出集上混合检索与重排均可将 Recall@5 提到 1.00 并修好 H01，但 H07/H08 同域无答案仍失败。详见 [08-RAG检索流水线与重排说明](08-RAG检索流水线与重排说明.md) §4 与 `docs/evidence/RAG/rag-eval-hybridrerank-master-summary-20261006.json`。
