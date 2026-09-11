# HHanClub 保种区自动化综合管理工具包

V2 版本：在固定的 **保种专项配额** 下，将当前保种任务与新候选统一进行组合优化，而不是单独评价某一个种子。

主要更新：
- 4TB 定义为 HHan `保种` 分类专项配额；
- 使用 0/1 背包在容量约束下选择价值最高组合；
- 配置化候选人数、批量下载、保护策略；
- 执行时先下载，再根据实际专项配额决定退出任务。

## 文件

- `hhan_pzone_manager.py`：核心管理脚本
- `config.example.json`：配置模板
- `requirements.txt`：Python 依赖
- `run.sh`：Linux / Synology NAS 入口
- `run.bat`：Windows 入口
- `run.ps1`：PowerShell 入口

## 策略说明

### 保种专项配额

`max_preservation_space_gb=4096`

仅约束 qBittorrent 中 HHan 且分类为 `保种` 的任务，不代表整块硬盘容量。

### 组合优化

```text
当前保种任务 + 新候选
          ↓
统一计算价值
          ↓
4TB 容量约束下背包优化
          ↓
目标组合
          ↓
分批收敛
```

主要参数：

| 参数 | 默认值 | 说明 |
|-|-|-|
| max_preservation_space_gb | 4096 | 保种专项配额 |
| min_candidate_seeders | 1 | 候选最低人数 |
| max_candidate_seeders | 3 | 候选最高人数 |
| max_batch_download_gb | 200 | 单轮新增上限 |
| max_batch_download_count | 10 | 单轮任务数上限 |
| portfolio_unit_gb | 0.1 | 背包离散粒度 |

## 使用

Dry Run：

```bash
python3 hhan_pzone_manager.py
```

执行：

```bash
python3 hhan_pzone_manager.py --execute
```

同步档案：

```bash
python3 hhan_pzone_manager.py --sync
```

## 安全

以下文件不会提交：

- config.json
- 日志
- 保种历史缓存
