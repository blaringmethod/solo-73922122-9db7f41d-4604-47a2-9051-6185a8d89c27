# 建设年报改造完工楼栋补算 API

按**现行区界**补算历年改造完工楼栋数的后端服务。给定各期旧区完工底账、新旧区
重叠范围的楼栋总数、各新区调档费用，在调档结果与底账相容的前提下，给出
**总费用最低的待调名单与金额**；照单调完后可**唯一**算出各新区完工数。

技术栈：FastAPI + SQLite（WAL）+ Pydantic，Docker Compose 启动。

## 1. 问题建模与关键结论

对每个期次：

- 旧区 `O` 有底账完工数 `c_O`；
- 新区 `N` 与旧区 `O` 的重叠范围有楼栋总数 `t_{N,O}`；
- 未知量 `x_{N,O}` = 现行新区 N 在原旧区 O 范围内的完工楼栋数，满足
  `0 ≤ x_{N,O} ≤ t_{N,O}`，且每个旧区 `Σ_N x_{N,O} = c_O`；
- 调一份新区 N 的档案，即得到该新区所有边上的 `x_{N,O}`，费用 `f_N`
  （正整数，每期最多 18 个新区）。

**何时能唯一确定全部未知量？**
底账相容时，只要每个旧区至多剩一条「未查」边即可：0 条未查边时余数必须为 0；
1 条时其值等于旧区完工余数。因此同一旧区下不允许同时存在两个未查新区——
这些新区两两构成**冲突**。待查集合必须覆盖冲突图的所有边，即冲突图的
**顶点覆盖**，目标是**费用最小的顶点覆盖**（等价于最大权独立集的补集）。
新区数 ≤ 18，服务用位掩码 DP 在 `O(2^k)`（实测 k=18 约 0.22 秒）内精确求解，
tie-break 规则：费用低 → 待调个数少 → 名单编码字典序小。

**底账相容性（登记校验，逐旧区独立）**

1. 各范围查回数介于 0 与该范围楼栋总数之间；
2. 已查总数 ≤ 旧区底账（完工余数非负）；
3. 完工余数 ≤ 未查楼栋总数（余数能由未查楼栋凑足）。

整数边容量 `0 ≤ x ≤ t` 下，条件 2、3 同时成立即保证存在可行分配。

## 2. 数据模型（SQLite）

| 表 | 关键字段 |
|---|---|
| `period` | 期次编码、名称 |
| `old_district` | 期次、编码、名称、底账完工数 |
| `new_district` | 期次、编码、名称、调档费用（>0） |
| `overlap` | 期次、新区、旧区、重叠楼栋总数（>0），三元组唯一 |
| `retrieval` | **旧格式**：期次、新区、旧区、查回完工数，每个范围一行（含 0） |
| `retrieval_archive` | **压缩格式档头**：期次、新区，一份档案一行 |
| `retrieval_detail` | **压缩格式明细**：档头、旧区、查回完工数（`> 0`），零值不落库 |

### 压缩档案存储

新登记的档案只写压缩格式：

- 一份档案 = `retrieval_archive` 一行档头；**档头存在即视为已登记**；
- 查回数非零的范围才写 `retrieval_detail`；**零计数不落库**；
- 全零档案只有一条档头、零条明细，**仍算已登记**——它参与底账相容性判断、
  不进入待调名单，也不被当作「尚未查回」。

查档（`GET .../retrievals`）会按该档案的重叠范围补零，**照常逐范围返回原数**。
计划和核算把两种状态分开返回：

- `retrieved_new`：已查回档案的新区，**包含查回数全为 0 的档案**；
- `unretrieved_new`：尚未查回档案的新区；
- 待调名单 `plan` 只会从 `unretrieved_new` 中产生。

旧格式 `retrieval`（升级前逐范围落库、零值也占一行）与新格式**可以同库共存**：
读取时合并两种格式（同一新区同时存在时以压缩档案为准），旧档案无需迁移即可查询、
参与计划与核算。

同值重送旧档案时，**只把这一份**旧档案转存为压缩格式（同一写事务内插入档头与
非零明细、删除该新区旧行）；其它旧档案不动。异值重送照旧 409 退回、不覆盖。
转存只要任一步失败就整体回滚，旧档案原样保留、仍可查到。

清空期次档案（`DELETE .../retrievals`）会把两种格式一起清空。

并发与事务：

- 开启 **WAL**：核算走只读快照（`BEGIN`），登记走写事务，二者不互斥；
- 所有登记使用 **`BEGIN IMMEDIATE`** 立即取写锁，并发登记被数据库串行化，
  配合 `busy_timeout=5000` 等待锁；
- 任一校验失败即 `ROLLBACK`，**原记录保留**；
- 同一新区相同结果重送幂等返回 201，异值返回 409 且不覆盖；
- 转存在写事务内先重读已提交账况：若期间被清空并重新登记了不同值，转存按异值
  退回，**不会覆盖清空后新登记的档案**；
- 计划/核算/查档端点在单个只读事务内读同一已提交状态，看不到未提交的转存中间态，
  核算期间登记照常进行。

## 3. API 一览（启动后见 `/docs` 交互文档）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/periods/` | 建立期次底账（旧区完工数、重叠楼栋总数、调档费用） |
| GET | `/periods/` | 期次列表 |
| GET | `/periods/{code}` | 期次底账详情 |
| GET | `/periods/{code}/plan` | **总费用最低的待调名单与金额**（只读快照） |
| GET | `/periods/{code}/accounting` | 核算；无需再调档时返回各新区完工数 |
| POST | `/periods/{code}/retrievals` | 登记一份新区档案（同值幂等、异值退回、旧档案同值转存） |
| GET | `/periods/{code}/retrievals` | 已登记档案（逐范围返回原数，含 0） |
| DELETE | `/periods/{code}/retrievals` | 清空该期登记（两种格式一起清空，便于在 /docs 重复试用） |

`/plan` 与 `/accounting` 的响应都带 `retrieved_new`（已查回，含查回为零的档案）
与 `unretrieved_new`（尚未查回），把「查回为零」和「尚未查回」分开。

## 4. 用 Docker Compose 启动

```bash
docker compose up --build
# 打开交互文档
# http://localhost:8000/docs
```

数据持久化在命名卷 `retrocalc-data`（容器内 `/data/app.db`）。
首次启动（库中还没有任何期次时）自动播种两个演示期次 `DEMO-2023` 与 `ZERO-2024`；
已有数据（含升级前的旧库）不会被播种或改写——升级后的旧库若想试用全零档案，
可自行 `POST /periods/` 建一个底账为 0 的期次。

旧格式与新格式同库共存，但一次只应运行一个版本的服务进程；滚动升级时请先停旧实例。
新登记只写压缩格式，旧版本读不到新档案。

本地无 Docker 时的等价运行方式：

```bash
python3 -m venv --without-pip .venv          # 按环境引导 pip 后
.venv/bin/pip install -r requirements.txt
APP_DB_PATH=./data/app.db .venv/bin/uvicorn app.main:app --port 8000
```

## 5. /docs 试用脚本（调档前 → 调档后核算）

演示期底账：旧区 O1=30、O2=24、O3=18、O4=10；
新区费用 N1=12、N2=7、N3=3、N4=10。
冲突图的最大权独立集为 {N1, N4}（可省 22），故**最低费用待调名单 = {N2, N3}，金额 10**。

```bash
B=http://localhost:8000/periods/DEMO-2023

# 调档前：拿到最低费用名单
curl -s $B/plan
# status=retrieval_required, plan=["N2","N3"], total_fee=10
curl -s $B/accounting        # determined=false，尚不返回完工数

# 登记 N2 档案（查回数必须覆盖该新区涉及的全部旧区 O1/O3）
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":10,"O3":6}}'
# 名单收缩为 ["N3"]，金额 3

# 登记 N3 档案
curl -s -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N3","counts":{"O2":14,"O3":2}}'
# status=completed，plan=[]

# 无需再调档，返回各新区完工数
curl -s $B/accounting
# N1=30, N2=16, N3=16, N4=20，合计 82 = 底账合计 30+24+18+10
```

校验类试用：

```bash
# 查回数超楼栋总数 -> 422
curl -i -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":16,"O3":0}}'
# 旧区集合不符 -> 422
curl -i -X POST $B/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":5}}'
# 同值重送 -> 201 幂等；异值重送 -> 409 原记录保留
# 完工余数无法由未查楼栋凑足 -> 409
```

## 5.1 /docs 试用：全零档案登记前后的计划与核算

演示期 `ZERO-2024`：旧区 O1 底账为 0，三个新区 N1(5) / N2(2) / N3(1) 都与 O1 重叠。

```bash
Z=http://localhost:8000/periods/ZERO-2024

# 登记前：三者两两冲突，最便宜覆盖 {N2, N3}，金额 3
curl -s $Z/plan
# status=retrieval_required, plan=["N2","N3"], total_fee=3
# retrieved_new=[], unretrieved_new=["N1","N2","N3"]
curl -s $Z/accounting      # determined=false，unretrieved_new 同上

# 登记 N1 的全零档案：查回数为 0，但档案已登记
curl -s -X POST $Z/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N1","counts":{"O1":0}}'

# 存储：retrieval_archive 1 行档头、retrieval_detail 0 行、retrieval 0 行
# 查档照常逐范围返回原数：
curl -s $Z/retrievals
# [{"new_code":"N1","old_code":"O1","completed_count":0}]
curl -s $Z/plan
# retrieved_new=["N1"]（查回为零，不再出现在名单）
# unretrieved_new=["N2","N3"], plan=["N3"], total_fee=1
curl -s $Z/accounting      # determined=false；仍差 N2/N3

# 再登记 N2 全零档案 -> O1 只剩 N3 一条未查边，可唯一核算
curl -s -X POST $Z/retrievals -H 'Content-Type: application/json' \
  -d '{"new_code":"N2","counts":{"O1":0}}'
curl -s $Z/accounting
# status=completed, determined=true,
# retrieved_new=["N1","N2"], unretrieved_new=["N3"]
# new_completed: N1=0, N2=0, N3=0；total_completed=0

# 清空后回到初始状态（两种格式一起清空）
curl -s -X DELETE $Z/retrievals
```

## 6. 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

- `tests/test_api.py`：建档校验、最低费用计划、四类登记校验、幂等/异值、
  计划推进、唯一核算、**真实 uvicorn 下的并发登记**、快照读不阻塞写；
- `tests/test_storage.py`：全零档案登记、零计数不落明细、旧/新格式同库共存、
  同值重送只转存该份、转存失败旧档案可查、清空两种格式、并发转存不覆盖新档案；
- `tests/test_solver.py`：18 新区完全图性能与正确性、链状图选便宜覆盖、
  脏数据触发 `infeasible` 防御、单未查边唯一求解。

## 7. 目录结构

```
app/
  db.py        # SQLite 连接、WAL、IMMEDIATE 写事务、只读快照
  schemas.py   # Pydantic 请求/响应模型
  services.py  # 相容性校验、最小权顶点覆盖 DP、唯一核算
  seed.py      # 演示期次种子数据
  main.py      # FastAPI 路由
tests/         # pytest 套件
Dockerfile  docker-compose.yml  requirements.txt  pytest.ini
```
