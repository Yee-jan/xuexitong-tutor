"""xxtutor - 学习通（超星/泛雅）作业自动答题框架。

模块划分：
    types      数据模型（Question / Answer / 题型枚举）
    config     配置加载与校验
    store      本地答案库（指纹去重，零 token 命中）
    llm        OpenAI 兼容 / Ollama / mock 客户端
    solver     缓存优先 + 批量压缩 Prompt 的答题调度器
    browser    Playwright 会话持久化
    page        学习通页面适配层（选择器 / 抓题 / 填答 / 提交）
    cli         命令行编排入口
"""

__version__ = "0.1.0"
