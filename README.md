# 大型储能电池全生命周期协同平台

本项目是一套可离线运行的 Python 服务端平台，服务于大型储能电池从入库、状态评估、组件检测、场站调拨到退役处置的协同管理。平台将资产流转、评估协议、质量决定、幂等结果和审计事件保存在 SQLite 中，供运营、质量、维修和审计人员在单个 Linux 应用容器内使用。

## 目录

- `src/battery_logistics/`：储能场站、调拨走廊、资产批次、容量申请、分配与处置情景；
- `src/battery_assurance/`：电池资产、证据版本、评估协议、观测导入、排除复核、分析任务与准入决定；
- `src/component_quality/`：电芯组件批次、响应测量、统计分析、账号权限和质量审批；
  并提供检测方法治理：方法版本冻结、仪器校准与参考样件比对门禁、原始读数溯源、
  不可变分析/报告版本、跨实验室系统偏差调查与后继方法复评；
- `fixtures/`：离线验收使用的评估协议与结构化观测；
- `tests/`：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 组件检测方法治理

`component_quality` 在原有批次检测链上增加方法治理层，解决三家实验室因校准批次与
判定算法不同导致结论相反、差异来源无法区分的问题：

- **方法发布即冻结**：`publish_method` 冻结适用产品、采样要求（点数/频率窗口）、指标公式、
  量程、判定阈值与跨实验室容差，内容规范化后生成 SHA-256 指纹；内容变更只能产生后继版本
  （旧版本转 `superseded`，亦可 `withdrawn`），已发布内容不可修改。
- **启用门禁**：实验室注册仪器 → 登记校准证书 → 参考样件比对在容差内，校准才为 `verified`。
  正式批次读数必须携带实验室、仪器身份与适用于该方法版本、在有效期内的已验证校准，
  且仪器须属于所声明实验室。
- **原始读数溯源**：读数保留原值、原单位、设备身份、实验室与校准证据；按方法版本声明的
  单位换算表折算规范单位并做量程校验。剔除、换算、合并都在分析版本中引用确定的
  `(method_id, revision)`。
- **不可变结论链**：分析结果只追加为带序号的版本（含输入指纹、剔除清单、换算记录、
  校准证书与方法指纹）；放行报告不可变，方法升级不会静默重算已出具报告。
- **跨实验室偏差调查**：同一样件比对超出方法容差时，系统自动暂停该方法版本下全部未决
  放行批次、冻结调查版本（含批次快照、实验室间差异）；暂停期间不能放行、补读数或用受疑
  版本重算。调查只能以同一方法的后继版本关闭，解除暂停后批次须按新版本复评，放行必须
  基于复评结论；历史报告保持原样。
- **委员会接口**：`GET /committee/overview` 汇总方法版本、校准证据、跨实验室比对、调查
  与仍被暂停的批次；`GET /lots/{id}/evidence` 给出单批次的方法指纹、原始读数、全部分析
  版本与报告证据链。

治理 HTTP 接口：`/methods`（发布/查询/撤回）、`/labs`、`/instruments`、
`/calibrations` 与 `/calibrations/{id}/reference-checks`、`/comparisons`、
`/investigations/{id}/close`，批次侧增加 `/lots/{id}/reports`、
`/lots/{id}/analysis`、`/measurements/{id}/exclusions`。方法与校准管理需要
`govern` 权限（quality/admin 角色）。历史无方法绑定的批次仍沿用旧计算与审批流程。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m battery_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m battery_assurance.acceptance --workspace .
PYTHONPATH=src python3 -m component_quality.acceptance
```

三条命令会在临时 SQLite 数据库中完成资产调拨、状态评估和组件质量流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m battery_logistics.api --database battery-logistics.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m battery_assurance.api --database battery-assurance.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_quality.api --database component-quality.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
