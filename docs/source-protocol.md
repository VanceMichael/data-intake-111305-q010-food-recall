# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `authority`、`case`、`revision`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

- `name`：situation；业务侧关注observable。
- `name`：situation；业务侧关注observable。
- `name`：situation；业务侧关注observable。

另外提供 `/rate-limited`、`/unstable`、`/range`、`/range-ignored`、`/truncated`、`/observations/1`、`/observations/2`、`/observations/3`、`/observations/4`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。

## 公告字段

来源记录的身份字段是 `authority`、`case`、`revision`。业务层据此解释公告，但不会把来源声明当作已解决的身份：

- `authority` 与 `case` 一起构成案件身份；不同 `authority` 下相同 `case` 是两个案件。
- `revision` 只用于排序修订，必须是非负整数。没有修订号的观察无法自动推进案件。
- `product` 是产品名。缺少产品名的观察需要人工核对。
- `lots` 是本次公告列出的批次；`null` 或空数组表示批次待补。字符串形式也接受，逗号、顿号、分号与换行都作为分隔符。
- `withdrawn_lots` 是明确撤回的批次，累积生效。
- `status` 缺省为 `published`。`corrected`、`amended`、`revised` 表示来源收窄范围：以前有效但本版未列出的批次视为撤回；`withdrawn`、`cancelled`、`canceled`、`retracted`、`closed`、`revoked` 表示整个案件撤回。

字段给出但格式错误（例如 `revision` 为字符串、`lots` 是对象）视为坏记录：接收会报告该条的下标与错误，但不写入案件。缺少字段本身不是坏记录，而是业务状态（待补或待核）。

