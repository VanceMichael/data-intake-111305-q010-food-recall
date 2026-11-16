# 食品召回批次公告的追补采集

这是资料接收程序。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。归档库只追加来源观察，不用最后一条响应覆盖先前响应；重复观察分别保留，原始内容按摘要复用。当前不自动执行门户提交。

在原始归档之上，`cases.py` 把公告观察接收为召回案件并计划追补：

- 案件身份只认来源声明的（机构、编号）；不同地区同编号各自成案，产品名相同不足以合案。
- 缺关键批次信息时案件保持待补，补齐批次后仍是同一案件；修订只前进不回退，旧页重现不恢复已撤范围。
- 门店库存批次与收件关系可动态变化；补发与更正只覆盖见过旧内容或当前受影响的门店，不对全体重发。
- 证据不足的观察进入待核，人工确认过的案件关联被保留。
- 通知的创建与发送分离，失败重试不产生重复更正；每一次通知、更正都能追到公告依据（归档捕获编号）。

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

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m food_recall serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m food_recall capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释可与原始接收独立演进。

## 案件接收与追补

在 `serve` 启动来源后，另开终端执行（地址替换为实际地址）：

```sh
python3 -m food_recall followup --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --casebook .runtime/cases.sqlite3 --source local-source --stores stores.json --dispatch
```

`--stores` 文件声明门店、订阅机构与库存批次，格式为 `{"stores": [{"id": "S1", "authorities": ["district-a"], "lots": ["L10"]}]}`，可随每次运行变化；`--replay` 改为重放归档中的全部捕获（幂等）；`--dispatch` 登记送达待处理通知。输出包含案件状态、通知计划、待核队列与各门店当前有效范围。门店查询用 `CaseBook.store_scope`，运营审计用 `CaseBook.case_timeline`，人工归并用 `CaseBook.confirm_association`。

## 开发资料

来源协议见 `docs/source-protocol.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径，`tests/test_cases.py` 会逐条执行这些情景并核对案件状态与通知范围。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `food_recall` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m food_recall demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
