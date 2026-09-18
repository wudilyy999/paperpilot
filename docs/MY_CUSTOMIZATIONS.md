# 项目定制记录

本文档记录本项目的个人定制与维护方式。

## 当前定制

| 定制 | 位置 | 说明 |
| --- | --- | --- |
| 项目元数据 | `setup.py`、`README.md`、全部 `docs/*.md` | 项目地址 `liyuyang/paperstorm-agent`，作者 liyuyang |
| 前端品牌化 | `frontend/paperstorm_dashboard/index.html`、`styles.css` | 顶栏徽标 `LY`、副标题 `LIYUYANG · RESEARCH AGENT`、版本徽章 `v7.3-ly.1`、侧边栏署名 |
| 许可证 | `LICENSE` | 保留 Stanford OVAL 原始版权声明（MIT 要求），追加修改版版权行 |

## 开发方向（按价值排序）

1. **LLM/检索配置收拢**：`.env` 或配置文件里集中管理 DeepSeek/arXiv/embedding 等密钥与端点，避免散落在代码里。
2. **个人文献工作流**：接入自己的 Zotero 库与本地 PDF 目录，固化一套默认的调研 persona 和写作风格。
3. **前端仪表盘**：`frontend/paperstorm_dashboard/` 是纯静态页面，可直接改配色、布局、默认 workspace；`sample_data.json` 替换成自己的真实调研数据。
4. **评测口径**：`tests/` 和 `knowledge_storm/evaluation/` 里有现成的 QASPER/SciFact benchmark，可按自己的论文方向换评测集。

## 快速运行

```bash
pip install -r requirements.txt
python -m pytest tests/            # 全量测试
open frontend/paperstorm_dashboard/index.html   # 直接预览前端仪表盘
```
