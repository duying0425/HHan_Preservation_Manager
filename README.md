# HHanClub 保种区自动化综合管理工具包

V4：在固定 **保种专项配额** 下，将当前保种任务与新候选统一进行组合优化；同时使用持续更新的保种账本，自动维护当前状态。

## 主要更新

- 4TB 定义为 HHan `保种` 分类专项配额；
- 使用 0/1 背包在容量约束下选择价值最高组合；
- `user_preservation_cache.json` 作为唯一运行时主账本；
- 每次运行自动与 qBittorrent 当前 HHan 任务对账；
- 手动将“待删除”任务恢复为“保种”后，会自动重新进入组合优化；
- 如果缓存文件不存在，会自动通过 HHan action=7 + 当前 qBittorrent 状态重建；
- 支持独立缓存刷新任务，不下载、不修改分类。

## 文件

- `hhan_pzone_manager.py`：核心管理脚本
- `config.example.json`：配置模板
- `requirements.txt`：Python 依赖
- `run.sh`：Linux / Synology NAS 入口
- `run.bat`：Windows 入口
- `run.ps1`：PowerShell 入口

运行数据：

- `user_preservation_cache.json`：唯一主账本（不提交 Git）

## 缓存账本机制

每次正常运行：

```text
user_preservation_cache.json
        ↓
检查 HHan action=7 远端数据是否超过刷新周期
        ↓
读取 qB 当前 HHan 任务
        ↓
更新 active / category / 当前人数 / 大小 / last_seen
        ↓
保存主账本
        ↓
执行组合优化
```

如果不存在缓存：

```text
首次启动
    ↓
强制刷新 HHan action=7
    ↓
读取 qB 当前任务
    ↓
自动生成 user_preservation_cache.json
```

支持：

- 手动恢复“待删除”任务；
- qB 任务状态变化自动同步；
- 不依赖旧历史快照文件。

## 单独刷新缓存

只更新缓存，不下载、不修改分类：

```bash
python3 hhan_pzone_manager.py --refresh-cache
```

旧参数仍兼容：

```bash
python3 hhan_pzone_manager.py --sync
```

Synology 定时任务示例：

```bash
cd /volume2/homes/duyingfang/HHan_Preservation_Manager
bash run.sh --refresh-cache
```

## 保种专项配额

默认：

```json
"max_preservation_space_gb": 4096.0
```

仅约束 qBittorrent 中 HHan 且分类为 `保种` 的任务，不代表整块硬盘容量。

## 组合优化

```text
当前保种任务 + 新候选
          ↓
统一计算 value_per_gb / portfolio_value
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
| cache_remote_refresh_hours | 24 | HHan 远端档案刷新周期 |

## 使用

Dry Run：

```bash
python3 hhan_pzone_manager.py
```

执行：

```bash
python3 hhan_pzone_manager.py --execute
```

缓存刷新：

```bash
python3 hhan_pzone_manager.py --refresh-cache
```

## 安全

以下文件不会提交：

- config.json
- 日志
- 保种历史缓存
