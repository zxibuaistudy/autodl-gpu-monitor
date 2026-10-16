# AutoDL GPU 监控与同地区实例克隆

自动监控 AutoDL 的 GPU 库存，可选择仅通知、自动创建按量实例，或将已关机实例的系统盘与数据盘克隆到同地区的新实例。

> **注意：自动购买和正式克隆会创建计费实例。** 首次使用请先核对账号、地区、GPU 型号、磁盘容量和费用；克隆模式建议先用 `dry_run: true` 演练。项目使用 AutoDL 网页内部接口，接口变更可能导致程序失效。本项目不是 AutoDL 官方工具。

## 功能与边界

- `monitor_only`：监控指定 GPU 型号，有库存时通知并退出，不创建实例。
- `monitor_and_buy`：监控库存并自动创建按量实例；使用 `image` 配置的公共或私有镜像。
- `monitor_and_clone`：从 AutoDL 返回的地区目录读取源实例的 `data_center`，只检查**同地区**且支持克隆的目标区域；找到兼容机器后克隆系统盘并复制数据盘。此模式使用源实例快照，`image` 配置不决定克隆镜像。
- 可选 Server酱通知；未配置 `sendkey` 时仅在终端输出。

克隆模式要求源实例属于当前账号、已关机，且 `clone.copy_data_disk` 为 `true`。程序不会自动关机或删除源实例。当前仅支持创建新的按量实例，不支持跨地区迁移，也不能保证目标机器一直有库存。

## 安装

建议使用 Python 3.9 或更新版本。进入项目目录后：

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Linux/macOS 可用 `source venv/bin/activate` 激活环境，并用 `cp config.example.json config.json` 复制模板。

编辑本机的 `config.json`。它包含登录令牌及可能的通知密钥，已被 Git 忽略，**不要上传或发给他人**。首次运行时，若 `authorization` 留空，程序会打开微信登录二维码，并把获取的令牌保存到本机 `config.json`。然后运行：

```powershell
python main.py
```

## 配置

完整字段见 [config.example.json](config.example.json)。模板默认是不会下单的 `monitor_only`，克隆演练也默认开启。常用字段：

| 字段 | 说明 |
| --- | --- |
| `mode` | `monitor_only`、`monitor_and_buy` 或 `monitor_and_clone` |
| `gpu.model` | AutoDL 使用的 GPU 型号名称，例如 `RTX 4090` 或 `vGPU-32GB` |
| `gpu.num` | 需要的 GPU 数量 |
| `image.private_image_uuid` | 普通购买模式的私有镜像 UUID；留空使用公共镜像 |
| `image.autodl_image` | 普通购买模式的公共镜像地址 |
| `notification.sendkey` | 可选的 Server酱 SendKey；留空不推送 |
| `clone.source_instance_uuid` | 要克隆的源实例 UUID |
| `clone.copy_data_disk` | 完整克隆必须为 `true` |
| `clone.dry_run` | `true` 只检查并打印候选请求；`false` 才正式提交 |
| `clone.optimize_sparse_files` | 是否启用稀疏文件优化 |
| `clone.expand_data_disk_gb` | 可选，目标数据盘扩容量（GiB）；不填时继承源实例的扩容量 |
| `clone.instance_name` | 可选，新实例名称 |

### 同地区完整克隆

1. 在 AutoDL 网页关闭源实例，确认数据已保存，复制该实例的 UUID。
2. 在 `config.json` 中设置 `mode` 为 `monitor_and_clone`，填写 `gpu.model`、`gpu.num` 和 `clone.source_instance_uuid`；保持 `copy_data_disk: true`、`dry_run: true`。
3. 运行 `python main.py`。程序会验证源实例状态、同地区目标机器、空闲 GPU、磁盘容量和镜像兼容性，然后打印候选请求，**不创建实例**。如果暂时没有合适库存，它每 30 秒重查。
4. 确认请求内容、账户余额和目标费用后，才把 `dry_run` 改为 `false` 并重新运行。提交后程序会跟踪数据盘复制状态。

示例配置片段（需与模板其余字段合并）：

```json
{
  "mode": "monitor_and_clone",
  "gpu": {"model": "vGPU-32GB", "num": 1},
  "clone": {
    "source_instance_uuid": "填写你自己的源实例 UUID",
    "copy_data_disk": true,
    "dry_run": true,
    "optimize_sparse_files": true
  }
}
```

所谓“同地区”以 AutoDL 当前返回的 `data_center` 为准，一个地区可能有多个不同的区域标识（`region_sign`）。克隆模式不使用普通监控/购买模式的静态 GPU 地区表。如果源地区在目录中不存在或不支持克隆，程序会报错停止，而不会尝试跨地区创建。

### 中断、失败与重复提交

正式提交前程序会写入本机 `clone_state.json`，并用 `clone_state.lock` 防止两个克隆进程同时运行。提交成功后重启程序可以继续跟踪现有目标实例；如果提交结果不明、失败或已经完成，程序会停止，避免自动重复下单。**不要直接删除状态文件来解决报错**：先到 AutoDL 网页核对目标实例和账单，确认是否需要新任务，再备份或移走 `clone_state.json`。全新演练不会创建克隆任务记录；如果是从已有拒绝记录恢复，演练会保留更新后的监控状态。

如果 AutoDL 明确返回 `FacilityNotSupported`（目标机器暂不支持克隆），或返回 `InternalError` 且消息**精确等于**“该主机已租满，请更换主机重新创建实例”，程序会记录并跳过该机器，继续寻找同地区的其他候选；旧版留下的同类 `rejected` 记录也能恢复监控，并保留之前的跳过列表。跳过列表保存在 `clone_state.json`，重启后仍生效。如果所有候选机器都在跳过列表中，程序会继续等待新机器；要重新考虑这些机器，应先在 AutoDL 网页确认没有未完成的克隆或订单，再将状态文件移出项目目录备份。其他 `InternalError`、拒绝或结果不明的请求不会自动重试。

## 验证与常见问题

```powershell
python -m unittest test_clone -v
```

- 提示“源实例状态”错误：先在 AutoDL 网页将源实例关机。
- 提示“找不到源实例”：检查 UUID 是否属于当前登录账号。
- 提示“源地区没有可克隆的目标区域”：检查源地区是否仍支持网页克隆；地区规则以 AutoDL 当前接口为准。
- 暂时没有符合条件的机器：程序会继续监控同地区库存；请确认 GPU 型号、数量和目标数据盘容量要求。
- `config.json` 缺失：从 `config.example.json` 复制一份，再填写自己的配置。

## 致谢与授权说明

本仓库是 [Jiayi-Fu/autodl-gpu-monitor](https://github.com/Jiayi-Fu/autodl-gpu-monitor) 的 GitHub Fork；原有监控与购买功能部分参考了 [autogpu](https://github.com/lemonmindyes/autogpu)。原仓库和参考项目目前没有明确的开源许可证，本 Fork 也不擅自为其代码添加许可证。公开可查看及在 GitHub 上 Fork，并不等于获得任意复制、修改和再分发的许可；若计划在 GitHub Fork 之外复用或发布这些代码，请先联系相应作者确认授权。
