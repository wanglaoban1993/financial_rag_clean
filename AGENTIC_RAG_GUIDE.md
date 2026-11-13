# Agentic RAG - 快速参考指南

## 概述
financial_rag 已成功从管道式 RAG 迁移到代理式 RAG，使用工具调用模式。

## 使用方法

### 1. 启动 Agent 模式
```bash
cd C:\path\to\financial_rag
conda activate shap_new
python query.py --agent
```

### 2. Agent 可用的工具
Agent 自动识别和调用以下工具：

| 工具 | 触发条件 | 返回信息 |
|-----|--------|--------|
| `search_documents()` | 用户询问文档、报告、信息 | 索引中的相关段落 |
| `get_market_quote()` | 用户询问股票价格、行情 | 实时市场数据 |
| `list_indexed_sources()` | 用户询问索引中有什么 | 文档统计信息 |

### 3. 交互式查询示例
```
你好，你是谁?
→ Agent 不需要工具，直接回答

我的索引里有什么?
→ Agent 调用 list_indexed_sources 获取文档统计

查一下 SAP.DE 的价格
→ Agent 调用 get_market_quote 获取实时价格

搜索关于金融的文档
→ Agent 调用 search_documents 查询本地索引
```

## 架构设计

### 与Pipeline RAG的区别
| 方面 | Pipeline RAG | Agentic RAG |
|-----|-------------|------------|
| 查询路由 | 固定规则 (routing) | LLM 智能决策 |
| 工具调用 | 预定义规则 | 动态识别需要 |
| 适应性 | 低 (需要修改规则) | 高 (LLM 自适应) |
| 成本 | 低 (少数LLM调用) | 中 (工具调用+重新生成) |

### 技术栈
- **LLM**: MiniMax (OpenAI-compatible API)
- **框架**: LangChain 0.3.28
- **工具调用方式**: 手动JSON解析 (不依赖bind_tools)
- **向量存储**: FAISS (本地)
- **嵌入模型**: BAAI/bge-m3

## 文件结构
```
financial_rag/
├── rag/
│   ├── agent_retriever.py    ← Agent 核心逻辑
│   ├── agent_tools.py         ← 工具定义
│   ├── retriever.py           ← Pipeline RAG (原始)
│   └── ...
├── query.py                   ← 交互式查询界面
├── config.yaml               ← 配置 (MiniMax API密钥)
└── test_*.py                 ← 测试脚本
```

## 故障排查

### Q: Agent 没有调用工具
**A**: 检查：
1. 问题表述是否清晰（Agent 需要识别需要哪个工具）
2. config.yaml 中 minimax_api_key 是否正确
3. 查看调试输出 `[Agent] Processing: ...` 和 `[Tool]` 消息

### Q: 工具返回错误
**A**: 
- `search_documents`: 确保已运行 `python main.py` 构建索引
- `get_market_quote`: 检查网络连接和符号格式 (如 "SAP.DE")
- `list_indexed_sources`: 应该总是返回索引统计

### Q: 响应很慢
**A**: 
- MiniMax API 响应慢是正常的
- 可以调整 config.yaml 中的 temperature (降低随机性)
- 考虑使用本地 Ollama 作为备选

## 下一步改进
- [ ] 支持多工具链式调用
- [ ] 增加文档修改/删除工具
- [ ] 集成更多数据源 (数据库、API等)
- [ ] 性能优化 (缓存、批处理)
- [ ] 用户权限控制
