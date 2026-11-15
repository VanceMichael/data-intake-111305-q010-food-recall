# 食品召回批次公告的追补采集

这是召回公告的接收程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

在原始采集之上，程序解释公告形成**召回案件**，并实现：

- 公告观察 → 案件状态的推进（待补、生效、撤回）；
- 门店当前有效范围的查询；
- 只针对差量的通知、更正与撤回，以及失败重试；
- 人工确认的案件关联与待核队列；
- 从通知追回公告依据的审计。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q food_recall tests
python3 -m food_recall demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取、归档与案件接收，并输出原样记录；结束自动清理。

## 常驻来源与接收

```sh
python3 -m food_recall serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m food_recall capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
python3 -m food_recall ingest --archive .runtime/raw.sqlite3 --cases .runtime/cases.sqlite3 --source local-source
```

`capture` 只把响应追加进原始归档，不用最后一条响应覆盖先前响应。`ingest` 解码已归档的捕获并推进案件；重复执行是安全的，不会重复生成决定或通知。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`cases.py` 负责案件与通知，`intake.py` 把两者接起来，`origin.py` 提供协议演练。

## 业务口径

- **案件身份**：来源声明 `(authority, case)`。`revision` 只表示修订序号，不是案件身份。不同地区即使编号相同也是不同案件。
- **待补**：公告缺少批次时不产生可执行范围；同一 `(authority, case)` 下补齐批次后仍是同一案件，受众不会重建。
- **待核**：缺少案件身份（例如只有产品名）或自动证据互相冲突时，观察进入待核队列。相同产品名不会被自动合案；同产品名的已有案件只作为候选提示。人工确认的归属会持久保存，并优先于来源声明。
- **有效范围**：批次跨修订累积。只有公告的 `withdrawn_lots` 或明确的收窄状态（如 `corrected`）才会移出批次；旧页重现、更高修订再次列出都不会恢复已撤批次，重新列出会进入待核。
- **通知受众**：逐门店、逐收件人记录**已送达**的内容。首发只发给当前持有有效批次的门店；后续更正只发差量给见过旧内容且范围受影响的门店，未见过旧内容或不受影响的门店不重复收到。门店库存与收件关系可以随时变化。
- **产品级公告**：批次尚未公布时可以只发产品名，让门店开始排查。补齐批次后，见过产品级公告且持有受影响批次的门店收到追补；补齐后不受影响的门店收到一次办结通知，不会继续按旧页排查。
- **幂等与重试**：每条通知由内容决定幂等键。投递失败重试复用同一通知与同一键，不新增更正记录；未送达的通知再次派发时重试原通知而不是新建。

## 命令

```sh
# 登记门店、收件人与当前库存（重复执行会替换该产品库存）
python3 -m food_recall store --cases .runtime/cases.sqlite3 --store S1 \
  --contact manager@example.invalid --product 合成谷物棒 --lot L10 --lot L11

# 门店当前有效范围（不会包含撤回批次、待补或待核案件）
python3 -m food_recall scope --cases .runtime/cases.sqlite3 --store S1

# 单个案件的范围与受影响门店
python3 -m food_recall scope --cases .runtime/cases.sqlite3 --case district-a/RC-8

# 待核队列；人工确认一条观察归属
python3 -m food_recall review --cases .runtime/cases.sqlite3
python3 -m food_recall review --cases .runtime/cases.sqlite3 --resolve 12:district-a/RC-8 --actor op-1 --reason "门户核对"

# 先看计划（解释每个受众为何收到或不再收到），再生成与投递
python3 -m food_recall dispatch --cases .runtime/cases.sqlite3 --case district-a/RC-8
python3 -m food_recall dispatch --cases .runtime/cases.sqlite3 --case district-a/RC-8 --send

# 批次未知时只发产品级公告；批次补齐后按追补/办结处理
python3 -m food_recall dispatch --cases .runtime/cases.sqlite3 --case district-a/RC-8 --advisory --send

# 审计：决定台账、通知、公告依据与逐次投递结果
python3 -m food_recall audit --cases .runtime/cases.sqlite3 --case district-a/RC-8
```

`--send` 在本地演练中使用空发送器，只记录幂等键，不连接任何真实供应商。

## 开发资料

来源协议见 `docs/source-protocol.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径。`tests/test_cases.py` 覆盖案件推进、范围、通知、待核、人工确认、审计与投递重试。

## Wheel 分发

构建产物位于 `dist/`，包含 `food_recall` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m food_recall demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
