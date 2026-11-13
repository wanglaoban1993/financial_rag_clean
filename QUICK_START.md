# 🚀 Agentic RAG - 快速开始

## 5 秒上手

```bash
# 1. 进入项目目录
cd C:\path\to\financial_rag

# 2. 激活环境
conda activate shap_new

# 2.1 设置配置文件路径（推荐放仓库外）
export FIN_RAG_CONFIG="$HOME/.config/financial_rag/config.yaml"

# 3. 启动 Agent 模式
python query.py --agent

# 4. 开始交互
>>> 我有多少个文档?
>>> 查一下 SAP.DE 的价格  
>>> /quit
```

## 三种查询模式

### 1️⃣ 无需工具的问题
```
输入: 你好，你是谁?
输出: [Agent] 直接生成答案，无工具调用
```

### 2️⃣ 工具触发的问题
```
输入: 我的索引里有多少文档?
流程: 
  [Agent] Processing...
  [Tool] list_indexed_sources({})
  [Result] 935 chunks from PDF
  → Agent 使用结果生成最终答案
输出: 您的索引中有 935 个文档段落...
```

### 3️⃣ 多工具链式调用（未来）
```
输入: 分析一下 SAP.DE 和我的投资报告中是否有提及
流程:
  → 调用 get_market_quote("SAP.DE")
  → 调用 search_documents("SAP")
  → 整合两个结果
输出: [Composite result]
```

## 可用的工具

### 工具 1: 搜索文档
```
触发词汇: "文档", "搜索", "查找", "关于", "分析"
例: "搜索一下关于金融的文档"
返回: 相关段落 + 来源标签
```

### 工具 2: 获取市场行情
```
触发词汇: "价格", "行情", "查一下", "报价", "$"
例: "查一下 AAPL 和 SAP.DE 的价格"
返回: 实时报价 (符号, 价格, 涨跌%)
```

### 工具 3: 列出索引源
```
触发词汇: "索引", "有什么", "库存", "统计", "多少"
例: "我的索引里有什么?"
返回: 文档类型和数量统计
```

## 常见问题

### Q: 怎样退出?
```
输入: /quit  或  exit  或  Ctrl+C
```

### Q: 怎样清空对话历史?
```
输入: /clear
```

### Q: 为什么有时响应较慢?
```
可能原因:
1. MiniMax API 响应延迟 (正常, 1-5秒)
2. 工具执行时间 (搜索大型索引)
解决: 耐心等待, 网络稳定通常可以改善
```

### Q: 支持中文吗?
```
是的! 系统支持:
✅ 中文输入
✅ 英文输入  
✅ 德文系统提示
LLM 会自动检测并处理
```

### Q: 我能添加自己的工具吗?
```
可以! 编辑 rag/agent_tools.py:
1. 添加新的 @tool 装饰函数
2. 在 init_tools() 中注册
3. 重启 Agent
详见: AGENTIC_RAG_IMPLEMENTATION.md
```

## 监测工具调用

### 调试输出
系统会打印以下信息，帮助您理解执行过程：

```
[Agent] Processing: <用户查询>
[Tool] <工具名>(<参数>)        # 工具被调用
[Result] <结果前300字符>...   # 工具返回结果
```

### 示例输出
```
[Agent] Processing: 我的索引里有什么?

[Tool] list_indexed_sources({})
[Result] {"total_chunks": 935, "by_source": {"pdf": 935}}...
```

## 架构对比

### 原 Pipeline RAG
```
用户查询 
  → 固定规则判断
  → 调用对应工具
  → 生成答案
```

### 新 Agentic RAG ⭐
```
用户查询
  → LLM 判断需要哪个工具
  → 智能执行工具
  → LLM 整合结果
  → 生成智能答案
```

## 系统要求

- Python 3.11+
- 8GB RAM (最小), 16GB 推荐
- 互联网连接 (用于 MiniMax API)
- `FIN_RAG_CONFIG` 指向有效配置文件，且其中包含 MiniMax API Key

## 性能指标

| 指标 | 值 |
|-----|-----|
| 响应延迟 (无工具) | 1-3 秒 |
| 响应延迟 (有工具) | 2-5 秒 |
| 峰值吞吐量 | ~2 queries/min |
| 内存使用 | ~2GB (FAISS 索引 + 模型) |
| API 成本/查询 | ~0.02 CNY (取决于 token 数) |

## 完整文档

📚 详细文档:
- [AGENTIC_RAG_GUIDE.md](./AGENTIC_RAG_GUIDE.md) - 用户指南
- [AGENTIC_RAG_IMPLEMENTATION.md](./AGENTIC_RAG_IMPLEMENTATION.md) - 技术细节
- [rag/agent_retriever.py](./rag/agent_retriever.py) - 源代码

## 故障排除

### 问题: "UnicodeDecodeError"
```
解决: 系统使用 UTF-8 编码, 通常无需操作
```

### 问题: "Tool 执行失败"
```
排查:
1. 检查网络连接
2. 检查 FIN_RAG_CONFIG 指向的配置文件
3. 查看 [Tool] 输出中的错误信息
```

### 问题: "LLM 没有调用工具"
```
原因: LLM 判断不需要工具 (正常)
改善: 用更具体的问题措辞
例: 
  差: "SAP 怎么样?"
  好: "查一下 SAP.DE 的当前股价"
```

## 开发者指南

### 添加新工具
```python
# 在 rag/agent_tools.py 中添加

from langchain_core.tools import tool

@tool
def my_new_tool(param: str) -> str:
    """Tool description for LLM."""
    # Your implementation
    return result

# 然后在 init_tools() 中注册
```

### 调试 Agent
```bash
# 运行测试脚本
python test_integration_final.py

# 查看详细输出
python -u query.py --agent
```

---

🎉 **现在你已经准备好使用 Agentic RAG 了!**

有任何问题? 查看完整文档或查看源代码注释。

**Happy querying!** 🚀
