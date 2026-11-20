# 核验练习资料台

两阶段答卷用相同 `skill` 识别核验技能；同一 `student` 的配对按技能判断，不按题号。事实主张的正反和部分支持来自人工记录，不要求自然语言判断。
`version` 是实际可引用版本；可访问时间 `shown_from` 至 `shown_until` 是闭区间，空结束表示没有下线。`evidence` 是材料内部标出的证据件；独立性按证据件及已裁定的同源关系判断。待定关系和裁定可能在交卷后才到达，不据此假定学生曾见过后来上线的材料。
`seen` 来自课内发放或阅读登记，`opportunities` 来自上课名单，即使未交也存在。时点是两阶段使用的业务日整数。已有反馈记录带原理由和发出日。

## 资料格式

资料是匿名合成登记，不包含真实学生、真实成绩或外部访问地址。UTF-8 JSON，每张表是对象数组；`null` 表示该字段无已知值。`records/catalog.json` 记录每张表的字段。

- `versions`：网址与版本分开登记。 字段：`version`、`url`、`evidence`、`shown_from`、`shown_until`、`kind`。
- `claims`：core 是该题用到的证据件。 字段：`claim`、`skill`、`stage`、`core`。
- `opportunities`：学生参加机会；无提交仍然可有此行。 字段：`student`、`claim`、`teacher`、`due`。
- `seen`：实际见到材料的登记。 字段：`student`、`version`、`day`。
- `submissions`：答卷中单条主张的提交时间。 字段：`submission`、`student`、`claim`、`day`。
- `citations`：judgment 取支持、部分、相反、待定；minute 为答卷复核到达的业务日。 字段：`submission`、`version`、`judgment`、`minute`。
- `relations`：pending 为提出，confirmed 为裁定同源，discarded 为裁定无同源；同一争议后续行取代前一状态。 字段：`dispute`、`left`、`right`、`state`、`day`。
- `feedback`：已发反馈的当时说明。 字段：`submission`、`teacher`、`day`、`note`。
- `review_slots`：空值表示未预约；锁定席位是实际安排。 字段：`slot`、`day`、`teacher`、`locked_dispute`。

## 本地查看

使用 Python 3.11 标准库，无需安装包。用 `python3 record_reader.py` 检查并列出收到的表行数；也可传入同格式资料目录。用 `python3 -m unittest discover -s tests -v` 检查资料契约。读取入口不会计算分析结果。
