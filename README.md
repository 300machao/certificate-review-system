# 外检计量证书智能审核系统

本系统是专利《一种外检计量证书信息一致性智能审核方法及系统》的本机验证实现。它在 `127.0.0.1:8766` 运行，不发布公网，也不读取或回写质检数智平台。

系统实现“批量导入 → 二维码与版面双通道 → 冻结台账三方比较 → GLM 主审 → DeepSeek 独立仲裁 → 人工复核 → 审计留痕”。规则能够明确判断且满足全部安全门槛时不会调用文本模型；模型、二维码或关键字段异常时不会自动通过。

> 真实性边界：本系统审核信息一致性，不代替发证机构官方查询。未接入官方查询前，所有记录的真实性均显示 `UNVERIFIED（未验证）`。

## 快速使用

1. 双击 `启动网站.cmd`。
2. 浏览器打开 <http://127.0.0.1:8766>。
3. 选择 PDF、证书文件夹或安全 ZIP；可同时选择 JSON/CSV 冻结台账。
4. 查看批次进度、证书证据、三方字段、规则差异和人工复核任务。
5. 导出 CSV、JSON 或包含原件与审计校验结果的审计 ZIP。
6. 结束时双击 `停止网站.cmd`。停止脚本只会终止由本系统记录且命令匹配的 Python 进程。

也可在 PowerShell 中启动：

```powershell
cd "<PROJECT_ROOT>"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\start.ps1
```

项目启动脚本只使用 `.venv`，不会回退到 PATH 或用户 site-packages。首次建立环境时要求 Python 3.12.4；`requirements.lock.txt` 是本基线已验证的精确版本集合，范围声明仍保留在 `requirements*.txt` 中用于后续受控升级。

## 已实现的业务链

- PDF、图片、目录、安全 ZIP 批量导入；JSON/CSV 台账冻结。
- ZIP 路径穿越、UNC/盘符/ADS、Windows 保留名、符号链接、规范化重名、CRC、压缩比、数量与展开大小门禁。
- PDF 魔数、可读性、页数和 SHA-256 校验；原件按内容哈希只读保存。
- 相同内容只保存一个对象并复用千问识别缓存，但每个业务附件均保留独立记录。
- PDF 文字层提取与本地多尺度二维码解码；二维码原文不入库、不写日志，只保留载荷哈希和解析字段。
- 千问视觉按三页分块处理整份 PDF，输出字段值、页码、证据和置信度；跨块冲突转人工复核。
- NFKC、空白/连字符、日期、名称、编号等确定性归一化，以及二维码—版面—冻结台账三方比较。
- GLM 5.1 仅接收规则无法确定的差异；DeepSeek V4 不读取 GLM 结论，按原始比较证据独立仲裁。
- 两个文本模型均高置信判定高风险不一致时才自动不通过；模型未配置、失败、低置信或冲突均转人工。
- 人工复核决定版本化；修正字段不覆盖机器原始提取。
- SQLite WAL 持久化，审计事件追加写入并形成 SHA-256 前序哈希链。
- 仅生成台账拟处置记录，绝不回写质检数智平台。

## 模型配置

推荐直接在网站的“API 设置”区域配置。每个模型只需要填写当前实现真实支持的三个字段：

- API 基础地址：例如 `https://gpu-api.dongfang.com/v1`，不要包含固定请求路径 `/chat/completions`。
- 模型名称。
- API Key：密码框默认隐藏；修改时输入新值，保持不变时留空。

点击“保存 API 设置”后立即重载模型客户端，不需要重启网站。各模型的“测试连接”会使用当前表单值发起一次最小 JSON 请求，因此可能产生极少量模型调用费用；测试不会保存表单中的新 Key，需另行点击保存。

保存位置与安全边界：

- 地址和模型名称：`data\settings\model-providers.json`。
- API Key：`data\settings\model-api-keys.dpapi.json`，使用 Windows 当前用户 DPAPI 加密。
- API Key 不写入浏览器存储、源码、SQLite、日志或导出文件，GET 接口也不会回显密钥。
- 加密文件只能由保存它的 Windows 用户解密；迁移到其他账户后需要重新输入密钥。
- `data` 已被 `.gitignore` 排除，不会随源码提交。

环境变量方式仍保留作为兼容入口；若网站已加密保存密钥，网站保存值优先于环境变量。

三个模型使用公司 OpenAI 兼容网关，默认路由：

| 角色 | 默认模型 | 凭据环境变量 |
|---|---|---|
| 图像结构化 | `qwen35-397b-a17b-int8` | `CERT_QWEN_API_KEY` |
| 文本主审 | `glm-5.1` | `CERT_GLM_API_KEY` |
| 异构仲裁 | `deepseek-v4` | `CERT_ARBITER_API_KEY` |

也可以双击/运行 `配置新API凭据.ps1`，使用隐藏输入把凭据写入当前 Windows 用户环境变量；这种方式配置后须重启网站。

任何曾暴露、过期或来源不明的旧密钥都必须先在公司网关撤销，禁止再次使用。没有三枚新凭据时网站仍可完成本地预检、二维码、规则和人工复核，所有需要模型的记录安全转人工。

可选环境变量：

- `CERT_MODEL_BASE_URL`：三个模型共用网关，默认 `https://gpu-api.dongfang.com/v1`。
- `CERT_QWEN_BASE_URL`、`CERT_GLM_BASE_URL`、`CERT_ARBITER_BASE_URL`：单独覆盖网关。
- `CERT_QWEN_MODEL`、`CERT_GLM_MODEL`、`CERT_ARBITER_MODEL`：单独覆盖模型路由。
- `CERT_MODEL_MODE=disabled`：强制禁用所有外部模型调用。
- `CERT_MODEL_MODE=validation`：有相应新凭据时启用专利验证链（默认）。

`GET /api/models/health` 和 `GET /api/model-settings` 只读取非敏感配置状态，永不发外部请求。网站保存使用 `PUT /api/model-settings`，单模型测试使用 `POST /api/model-settings/test`；两个写操作均要求本机同源页面及专用操作标头。页面不会自动发起收费的连接测试。

## 私有冻结语料验证

`scripts/import_frozen_batch.py` 不搜索默认语料目录，也不在源码中保存现场批次数量、字节数或其他业务基线。新导入必须显式提供私有语料目录和期望门禁；默认仍完全离线，不调用任何模型：

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe .\scripts\import_frozen_batch.py `
  --corpus "<PRIVATE_CORPUS_DIR>" `
  --expectations ".\data\validation\corpus.expectations.local.json" `
  --batch-name "本地冻结语料验证"
```

期望文件只保存本地核准的整数门禁，包含 `records`、`unique_objects`、`duplicates`、`pages`、`bytes`、文字层分类和二维码分类。建议把它放在已忽略的 `data` 目录，或使用 `*.expectations.local.json` 文件名；也可通过脚本的 `--expect-*` 参数逐项提供。缺少任一门禁时脚本会在导入前停止。

只有在三枚新凭据均已配置、已明确授权并接受调用费用后，才可在同一条显式命令末尾增加 `--enable-models`。已有批次的只读复核同样必须显式提供本地期望门禁。

验证材料分为两类：

- `tests/fixtures/anomaly-fixtures-12.synthetic.json` 是可版本化的全合成规则夹具，不含真实编号、原件路径、原件哈希或模型结果。
- `validation` 及 `data\validation` 保存人工金标、现场派生夹具和运行结果，均属于私有业务材料并由 Git 排除。

私有验证结果不能作为通用产品基线提交。没有真实模型运行、权威验真和人工标注证据时，不宣称达到字段准确率目标或完成三模型效果验收。

## 状态与自动判定门槛

证书处理状态：

`UPLOADED → PRECHECKED → QR/VISION_RUNNING → COMPARING → AUTO_PASSED | SEMANTIC_REVIEW → MODEL_ARBITRATION | HUMAN_REVIEW | AUTO_FAILED → FINALIZED`

技术失败进入 `PROCESSING_FAILED`，可重新处理；旧提取运行和模型决定不会删除。

自动通过同时要求：关键字段完整且规则一致、结论合格、置信度不低于0.90、二维码多尺度稳定、无重复或其他异常。冻结台账缺失、关键字段缺失、二维码失败、模型失败或低置信时均不会自动通过。

## 数据与备份

- 数据库：`data\certificate-review.sqlite3`
- 只读原件对象：`data\storage\objects`
- 冻结验证报告：`data\validation`
- 运行进程标记：`data\server.pid`

长期保存数据时，应在网站停止后整体备份 `data` 目录。不要单独复制正在写入的 SQLite 主文件而忽略 WAL 文件。

## API

- `POST /api/batches`
- `GET /api/batches`、`GET /api/batches/{id}`
- `GET /api/certificates/{id}`、`GET /api/certificates/{id}/file`
- `POST /api/certificates/{id}/retry`
- `GET /api/review-tasks?status=OPEN`
- `POST /api/review-tasks/{id}/decision`
- `GET /api/models/health`
- `GET /api/model-settings`、`PUT /api/model-settings`
- `POST /api/model-settings/test`
- `GET /api/batches/{id}/export.json`
- `GET /api/batches/{id}/export.csv`
- `GET /api/batches/{id}/audit.zip`

## 测试

```powershell
$env:CERT_MODEL_MODE = "disabled"
$env:CERT_DATA_DIR = Join-Path ([System.IO.Path]::GetTempPath()) ("certificate-review-tests-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --check .\static\app.js
```

`tests/conftest.py` 会拒绝把测试数据指向项目树，并再次强制禁用模型、清空 API 密钥环境变量。自动测试全部使用本地夹具或模拟传输，不调用公司模型、不产生费用。测试覆盖规则、状态门禁、数据库恢复、审计链、ZIP安全、并发内容入库、模型非法JSON/超时/429/5xx、缓存、凭据脱敏和人工复核版本。

## 当前限制

- 本机单用户、无登录，不应改为局域网或公网监听。
- 未接发证机构官方验真接口，真实性始终未验证。
- 不写回、不冻结质检数智平台中的器具或台账。
- 扫描件在千问未配置或失败时进入人工复核。
- 第二文本模型路由或凭据缺失时，高风险差异进入人工复核，不宣称完成双模型仲裁效果验证。
