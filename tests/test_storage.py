"""压缩档案存储、旧档案兼容、转存与并发语义测试。

存储约定
--------
- 新格式：`retrieval_archive` 每份档案一行档头；
  `retrieval_detail` 只存非零范围，零计数不落库；全零档案只有档头。
- 旧格式：`retrieval` 每个范围一行（含零），保留读取；同值重送时转存。
- 清空：两种格式一起清空。
"""
from __future__ import annotations

import threading

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import services
from app.db import connect
from app.main import app

PERIOD = "DEMO-2023"
ZERO_PERIOD = "ZERO-2024"


# ---------------------------------------------------------------- 工具

def _period_id(conn, code: str) -> int:
    return conn.execute("SELECT id FROM period WHERE code = ?", (code,)).fetchone()["id"]


def _storage_counts(period_code: str) -> dict[str, int]:
    """返回该期三种档案表的行数：旧格式 / 压缩档头 / 非零明细。"""
    conn = connect()
    try:
        pid = _period_id(conn, period_code)
        legacy = conn.execute(
            "SELECT COUNT(*) AS c FROM retrieval WHERE period_id = ?", (pid,)
        ).fetchone()["c"]
        archive = conn.execute(
            "SELECT COUNT(*) AS c FROM retrieval_archive WHERE period_id = ?", (pid,)
        ).fetchone()["c"]
        detail = conn.execute(
            "SELECT COUNT(*) AS c FROM retrieval_detail d "
            "JOIN retrieval_archive a ON a.id = d.archive_id "
            "WHERE a.period_id = ?",
            (pid,),
        ).fetchone()["c"]
        return {"legacy": legacy, "archive": archive, "detail": detail}
    finally:
        conn.close()


def _insert_legacy(period_code: str, new_code: str, counts: dict[str, int]) -> None:
    """直接写入旧格式档案（每个范围一行，含 0），模拟升级前的历史数据。"""
    conn = connect()
    try:
        pid = _period_id(conn, period_code)
        nid = conn.execute(
            "SELECT id FROM new_district WHERE period_id = ? AND code = ?",
            (pid, new_code),
        ).fetchone()["id"]
        conn.execute("BEGIN IMMEDIATE")
        for old_code, value in counts.items():
            oid = conn.execute(
                "SELECT id FROM old_district WHERE period_id = ? AND code = ?",
                (pid, old_code),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO retrieval(period_id, new_district_id, old_district_id, "
                "completed_count) VALUES (?, ?, ?, ?)",
                (pid, nid, oid, value),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _retrieval_rows(client, period_code: str) -> dict[tuple[str, str], int]:
    rows = client.get(f"/periods/{period_code}/retrievals").json()
    return {(r["new_code"], r["old_code"]): r["completed_count"] for r in rows}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def server():
    config = uvicorn.Config(app, host="127.0.0.1", port=8766, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:8766/docs", timeout=1.0)
            break
        except httpx.TransportError:
            threading.Event().wait(0.1)
    yield "http://127.0.0.1:8766"
    srv.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------- 全零档案

def test_zero_archive_counts_as_registered(client):
    client.delete(f"/periods/{ZERO_PERIOD}/retrievals")

    # 登记前：3 个新区共享 O1（完全冲突图），最便宜覆盖 {N2(2), N3(1)}=3
    plan = client.get(f"/periods/{ZERO_PERIOD}/plan").json()
    assert plan["status"] == "retrieval_required"
    assert plan["plan"] == ["N2", "N3"]
    assert plan["total_fee"] == 3
    assert plan["retrieved_new"] == []
    assert plan["unretrieved_new"] == ["N1", "N2", "N3"]
    acc = client.get(f"/periods/{ZERO_PERIOD}/accounting").json()
    assert acc["determined"] is False
    assert acc["retrieved_new"] == []
    assert acc["unretrieved_new"] == ["N1", "N2", "N3"]

    # 登记全零档案：查回数为 0，但仍是一条已登记档案
    r = client.post(
        f"/periods/{ZERO_PERIOD}/retrievals",
        json={"new_code": "N1", "counts": {"O1": 0}},
    )
    assert r.status_code == 201

    # 存储：1 条档头、0 条范围明细、0 条旧格式
    assert _storage_counts(ZERO_PERIOD) == {"legacy": 0, "archive": 1, "detail": 0}
    # 查档照常逐范围返回原数（含 0）
    assert _retrieval_rows(client, ZERO_PERIOD) == {("N1", "O1"): 0}

    # 计划/核算：N1 属于「已查回（为零）」，不再出现在待调名单和未查列表
    plan = client.get(f"/periods/{ZERO_PERIOD}/plan").json()
    assert plan["retrieved_new"] == ["N1"]
    assert plan["unretrieved_new"] == ["N2", "N3"]
    assert plan["plan"] == ["N3"] and plan["total_fee"] == 1
    assert plan["registered"] == [
        {"new_code": "N1", "fee": 5, "counts": {"O1": 0}}
    ]
    acc = client.get(f"/periods/{ZERO_PERIOD}/accounting").json()
    assert acc["determined"] is False
    assert acc["retrieved_new"] == ["N1"]
    assert acc["unretrieved_new"] == ["N2", "N3"]

    # 再登记 N2 全零：O1 只剩 N3 一条未查边 -> 可唯一核算
    assert (
        client.post(
            f"/periods/{ZERO_PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 0}},
        ).status_code
        == 201
    )
    assert _storage_counts(ZERO_PERIOD) == {"legacy": 0, "archive": 2, "detail": 0}
    acc = client.get(f"/periods/{ZERO_PERIOD}/accounting").json()
    assert acc["status"] == "completed" and acc["determined"] is True
    assert acc["retrieved_new"] == ["N1", "N2"]
    assert acc["unretrieved_new"] == ["N3"]
    got = {item["new_code"]: item["completed"] for item in acc["new_completed"]}
    assert got == {"N1": 0, "N2": 0, "N3": 0}
    assert acc["total_completed"] == 0


def test_zero_counts_not_stored_but_nonzero_counts_are(client):
    client.delete(f"/periods/{ZERO_PERIOD}/retrievals")
    # N2 一条非零、N3 全零：只落非零明细
    assert (
        client.post(
            f"/periods/{ZERO_PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 0}},
        ).status_code
        == 201
    )
    assert _storage_counts(ZERO_PERIOD) == {"legacy": 0, "archive": 1, "detail": 0}
    client.delete(f"/periods/{ZERO_PERIOD}/retrievals")

    # DEMO 的 N2 有两条范围；把一个零值一起送来，零值不落明细
    client.delete(f"/periods/{PERIOD}/retrievals")
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 0}},
        ).status_code
        == 201
    )
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 1}
    # 查档照常逐范围返回原数：零值范围也要返回
    assert _retrieval_rows(client, PERIOD) == {
        ("N2", "O1"): 10,
        ("N2", "O3"): 0,
    }

    client.delete(f"/periods/{PERIOD}/retrievals")
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
        ).status_code
        == 201
    )
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    assert _retrieval_rows(client, PERIOD) == {
        ("N2", "O1"): 10,
        ("N2", "O3"): 6,
    }
    client.delete(f"/periods/{PERIOD}/retrievals")


# ---------------------------------------------------------------- 旧格式兼容

def test_legacy_and_compressed_formats_coexist(client):
    client.delete(f"/periods/{PERIOD}/retrievals")
    # 旧格式：N2（逐范围两行）
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})
    # 新格式：N3
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N3", "counts": {"O2": 14, "O3": 2}},
        ).status_code
        == 201
    )
    assert _storage_counts(PERIOD) == {"legacy": 2, "archive": 1, "detail": 2}

    # 两种格式都能查到，且数值一致
    assert _retrieval_rows(client, PERIOD) == {
        ("N2", "O1"): 10,
        ("N2", "O3"): 6,
        ("N3", "O2"): 14,
        ("N3", "O3"): 2,
    }
    plan = client.get(f"/periods/{PERIOD}/plan").json()
    assert plan["status"] == "completed"
    assert plan["retrieved_new"] == ["N2", "N3"]
    assert plan["unretrieved_new"] == ["N1", "N4"]
    acc = client.get(f"/periods/{PERIOD}/accounting").json()
    got = {x["new_code"]: x["completed"] for x in acc["new_completed"]}
    assert got == {"N1": 30, "N2": 16, "N3": 16, "N4": 20}

    # 清空：两种格式一起清空
    assert client.delete(f"/periods/{PERIOD}/retrievals").status_code == 200
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 0, "detail": 0}
    assert _retrieval_rows(client, PERIOD) == {}
    assert client.get(f"/periods/{PERIOD}/plan").json()["plan"] == ["N2", "N3"]


def test_same_value_resend_converts_only_that_legacy_archive(client):
    client.delete(f"/periods/{PERIOD}/retrievals")
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})
    _insert_legacy(PERIOD, "N1", {"O1": 20, "O2": 10})
    assert _storage_counts(PERIOD) == {"legacy": 4, "archive": 0, "detail": 0}

    # 同值重送 N2 -> 只转存 N2，N1 的旧档案原样保留
    r = client.post(
        f"/periods/{PERIOD}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
    )
    assert r.status_code == 201
    assert _storage_counts(PERIOD) == {"legacy": 2, "archive": 1, "detail": 2}
    assert _retrieval_rows(client, PERIOD) == {
        ("N1", "O1"): 20,
        ("N1", "O2"): 10,
        ("N2", "O1"): 10,
        ("N2", "O3"): 6,
    }

    # 转存后异值重送仍退回，压缩档案不被覆盖
    r = client.post(
        f"/periods/{PERIOD}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 9, "O3": 7}},
    )
    assert r.status_code == 409
    assert _retrieval_rows(client, PERIOD)[("N2", "O1")] == 10
    client.delete(f"/periods/{PERIOD}/retrievals")


def test_legacy_zero_row_converts_to_header_only(client):
    client.delete(f"/periods/{PERIOD}/retrievals")
    # 旧格式里 N2 的 O3 查回为 0：旧表仍占一行
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 0})
    assert _storage_counts(PERIOD) == {"legacy": 2, "archive": 0, "detail": 0}

    # 同值重送 -> 转存后零值不再占明细行
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 0}},
        ).status_code
        == 201
    )
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 1}
    # 查档照常逐范围返回原数（O3 仍为 0）
    assert _retrieval_rows(client, PERIOD) == {("N2", "O1"): 10, ("N2", "O3"): 0}
    client.delete(f"/periods/{PERIOD}/retrievals")


# ---------------------------------------------------------------- 转存失败

def test_conversion_failure_keeps_legacy_archive_readable(client, monkeypatch):
    client.delete(f"/periods/{PERIOD}/retrievals")
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟写入失败")

    monkeypatch.setattr(services, "_insert_archive", boom)
    with TestClient(app, raise_server_exceptions=False) as failing:
        r = failing.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
        )
        assert r.status_code == 500
    monkeypatch.undo()

    # 转存失败已回滚：旧档案仍在，查档/计划照常
    assert _storage_counts(PERIOD) == {"legacy": 2, "archive": 0, "detail": 0}
    assert _retrieval_rows(client, PERIOD) == {
        ("N2", "O1"): 10,
        ("N2", "O3"): 6,
    }
    assert client.get(f"/periods/{PERIOD}/plan").json()["retrieved_new"] == ["N2"]

    # 恢复后可正常转存
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
        ).status_code
        == 201
    )
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    client.delete(f"/periods/{PERIOD}/retrievals")


def test_stale_legacy_value_cannot_overwrite_newer_archive(client):
    """清空后重新登记了新值，旧格式的「同值」重送不能再覆盖它。"""
    client.delete(f"/periods/{PERIOD}/retrievals")
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})
    client.delete(f"/periods/{PERIOD}/retrievals")  # 清空两种格式

    # 新值与旧值不同，且单独登记也可行（O1 余 19 可由未查的 N1 容量 20 凑足）
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 11, "O3": 5}},
        ).status_code
        == 201
    )
    # 旧值重送 = 与新档案异值 -> 409，原档案保留
    r = client.post(
        f"/periods/{PERIOD}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
    )
    assert r.status_code == 409
    assert _retrieval_rows(client, PERIOD) == {("N2", "O1"): 11, ("N2", "O3"): 5}
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    client.delete(f"/periods/{PERIOD}/retrievals")


# ---------------------------------------------------------------- 并发

def test_reads_see_committed_state_while_conversion_in_flight(client, server, monkeypatch):
    """转存事务未提交时，查档端点仍只读到完整的旧账况。"""
    base = server
    httpx.delete(f"{base}/periods/{PERIOD}/retrievals", timeout=10)
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})

    entered = threading.Event()
    release = threading.Event()
    real_insert = services._insert_archive

    def paused_insert(*args, **kwargs):
        real_insert(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr(services, "_insert_archive", paused_insert)
    result: dict[str, int] = {}

    def convert():
        r = httpx.post(
            f"{base}/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
            timeout=20,
        )
        result["status"] = r.status_code

    thread = threading.Thread(target=convert, daemon=True)
    thread.start()
    assert entered.wait(timeout=10), "转存事务未进入暂停点"

    # 事务内已插入压缩档案但未提交：查档/计划只能看到旧的已提交账况
    rows = httpx.get(f"{base}/periods/{PERIOD}/retrievals", timeout=10).json()
    assert {(r["new_code"], r["old_code"]): r["completed_count"] for r in rows} == {
        ("N2", "O1"): 10,
        ("N2", "O3"): 6,
    }
    plan = httpx.get(f"{base}/periods/{PERIOD}/plan", timeout=10).json()
    assert plan["retrieved_new"] == ["N2"]

    release.set()
    thread.join(timeout=20)
    assert result["status"] == 201
    monkeypatch.undo()

    # 提交后转存完成：旧格式消失，压缩档案可查
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    httpx.delete(f"{base}/periods/{PERIOD}/retrievals", timeout=10)


def test_concurrent_conversion_cannot_overwrite_archive_registered_after_clear(
    client, server, monkeypatch
):
    """并发转存不得覆盖「清空后新登记」的档案。

    让转存请求在进入写事务前停住；期间清空并登记新值；再放行转存。
    转存必须读到新账况并以异值退回（409），不得用旧值覆盖新档案。
    """
    base = server
    httpx.delete(f"{base}/periods/{PERIOD}/retrievals", timeout=10)
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})

    entered = threading.Event()
    release = threading.Event()
    armed = threading.Event()
    armed.set()
    real_register = services.register_retrieval

    def gated_register(conn, period_code, payload):
        # 在进入 BEGIN IMMEDIATE 之前停住：模拟「读到旧值后、取写锁前」被清空抢先
        if armed.is_set():
            armed.clear()
            entered.set()
            assert release.wait(timeout=10)
        return real_register(conn, period_code, payload)

    monkeypatch.setattr(services, "register_retrieval", gated_register)
    result: dict[str, int] = {}

    def convert():
        r = httpx.post(
            f"{base}/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 10, "O3": 6}},
            timeout=20,
        )
        result["status"] = r.status_code

    thread = threading.Thread(target=convert, name="legacy-converter", daemon=True)
    thread.start()
    assert entered.wait(timeout=10), "转存请求未进入等待点"

    # 清空两种格式，然后登记新值
    assert httpx.delete(f"{base}/periods/{PERIOD}/retrievals", timeout=10).status_code == 200
    r_new = httpx.post(
        f"{base}/periods/{PERIOD}/retrievals",
        json={"new_code": "N2", "counts": {"O1": 11, "O3": 5}},
        timeout=10,
    )
    assert r_new.status_code == 201

    release.set()
    thread.join(timeout=20)
    monkeypatch.undo()

    # 旧值转存被退回，新档案完好
    assert result["status"] == 409
    rows = httpx.get(f"{base}/periods/{PERIOD}/retrievals", timeout=10).json()
    assert {(r["new_code"], r["old_code"]): r["completed_count"] for r in rows} == {
        ("N2", "O1"): 11,
        ("N2", "O3"): 5,
    }
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    httpx.delete(f"{base}/periods/{PERIOD}/retrievals", timeout=10)


def test_second_conversion_is_idempotent(client):
    client.delete(f"/periods/{PERIOD}/retrievals")
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})
    body = {"new_code": "N2", "counts": {"O1": 10, "O3": 6}}
    assert client.post(f"/periods/{PERIOD}/retrievals", json=body).status_code == 201
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    # 已转存后的同值重送：幂等，不再新增档头/明细
    assert client.post(f"/periods/{PERIOD}/retrievals", json=body).status_code == 201
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    client.delete(f"/periods/{PERIOD}/retrievals")


def test_compressed_archive_wins_when_both_formats_present(client):
    """同库两种格式同时存在时，读取以压缩档案为准（避免旧值覆盖新值）。"""
    client.delete(f"/periods/{PERIOD}/retrievals")
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 11, "O3": 5}},
        ).status_code
        == 201
    )
    # 直接补一份旧格式的旧值（模拟历史遗留/复制过来的数据）
    _insert_legacy(PERIOD, "N2", {"O1": 10, "O3": 6})
    assert _storage_counts(PERIOD) == {"legacy": 2, "archive": 1, "detail": 2}

    # 查档返回压缩档案的值
    assert _retrieval_rows(client, PERIOD) == {("N2", "O1"): 11, ("N2", "O3"): 5}
    # 同值重送（与新档案一致）只清理旧格式，压缩档案保持不变
    assert (
        client.post(
            f"/periods/{PERIOD}/retrievals",
            json={"new_code": "N2", "counts": {"O1": 11, "O3": 5}},
        ).status_code
        == 201
    )
    assert _storage_counts(PERIOD) == {"legacy": 0, "archive": 1, "detail": 2}
    assert _retrieval_rows(client, PERIOD) == {("N2", "O1"): 11, ("N2", "O3"): 5}
    client.delete(f"/periods/{PERIOD}/retrievals")


# ---------------------------------------------------------------- 文档契约

def test_openapi_builds_with_storage_fields(client):
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    schemas = spec.json()["components"]["schemas"]
    assert "retrieved_new" in schemas["PlanOut"]["properties"]
    assert "unretrieved_new" in schemas["AccountingOut"]["properties"]
