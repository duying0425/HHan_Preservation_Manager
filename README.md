# HHanClub 保种区自动化综合管理工具包

## 一、 文件清单与结构说明
- **`hhan_pzone_manager.py`**：核心管理流水线 Python 脚本，支持 Windows / Linux (群晖 NAS) 跨平台运行。
- **`config.example.json`**：配置模板文件。实际运行时复制为 `config.json` 并填入认证凭据。
- **`run.sh`**：Linux / 群晖 NAS 环境下的自动化运行脚本。
- **`run.bat`**：Windows 环境下一键执行入口。
- **`run.ps1`**：Windows PowerShell 执行后台，实时屏幕打印并写入日志。
- **`logs/manager.log`**：运行日志文件，每次执行均会自动带时间戳追加写入。

---

## 二、 核心策略原则
1. **【宁缺毋滥，拒绝平庸】**：
   - 绝不下载 0 人死种（无做种源，无法完成下载，7 天会被系统惩罚取消名额）；
   - 绝不下载 4~5 人满员种（只有 1.5x 低倍率，且濒临踢出保种区）；
   - 仅在保种区出现 1~2 人极品神种 (Tier 0 / Tier 1，锁定 2.0x/1.75x 积分) 时才建议下载。
2. **【4TB 容量水位管理】**：
   - 设定保种分类总空间预算为 4.0 TB (4,096 GB)；
   - 当前占用 < 4TB 且无极品新种时：静默守护，不下载、不删除；
   - 当前占用 < 4TB 且有极品新种时：直接下载吸纳，无需淘汰老种；
   - 当（当前占用 + 新增下载）超出 4TB 时：按评分/DPI 从最差到最好，精准淘汰最劣质老种标记为【待删除】，将总容量压回 4TB 以内。

---

## 三、 配置与使用方法

### 1. 配置认证信息
将 `config.example.json` 复制为 `config.json`，并填入您的站点与客户端信息：
```json
{
  "hhan_base_url": "https://hhanclub.net",
  "hhan_cookie": "YOUR_COOKIE",
  "hhan_passkey": "YOUR_PASSKEY",
  "user_id": "YOUR_UID",
  "qb_base_url": "http://127.0.0.1:8080",
  "qb_cookie": "SID=YOUR_SID",
  "max_preservation_space_gb": 4096.0
}
```

### 2. 命令行指定模式运行
- **安全只读巡检（Dry-Run 模式，不改动任何数据）**：
  ```bash
  python3 hhan_pzone_manager.py
  ```
- **实际执行（Execute 模式）**：
  ```bash
  python3 hhan_pzone_manager.py --execute
  ```
- **强制全量重新在线同步站点保种档案（Sync 模式）**：
  ```bash
  python3 hhan_pzone_manager.py --sync
  ```

---

## 四、 群晖 NAS 部署与定时任务

在群晖 NAS 用户主目录下部署，并通过定时任务（例如每 10 分钟一次）全自动巡检与收割保种区。

1. **执行入口**：`bash run.sh`
2. **定时任务**（Crontab）：
   ```crontab
   */10 * * * * duyingfang /bin/bash /volume2/homes/duyingfang/HHan_Preservation_Manager/run.sh
   ```
