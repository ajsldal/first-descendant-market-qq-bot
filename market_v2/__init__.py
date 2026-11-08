"""Nexon TFD 交易市场 v2 条件筛选抓取器（独立项目）。

只依赖公开只读接口：
  - Jarvis 物品字典（名称 -> ItemId / moduleType）
  - POST /api/v2/trademarket/{lang}/products（服务端筛选）
  - POST /api/v2/trademarket/{lang}/filter（名称 -> 属性 ID）
"""

__version__ = "1.0.0"
