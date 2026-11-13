"""2026-11-13：研究用官方資料每日快照——原樣保存、只存有變的版本、被擋不算成功、公開端點只讀。"""
import json
from datetime import datetime, timedelta, timezone

import research_archive as ra

TPE = timezone(timedelta(hours=8))


def _fake(payloads):
    def f(url):
        v = payloads.get(url, b"[]")
        if isinstance(v, Exception):
            raise v
        return v
    return f


def test_snapshot_saves_raw_dedupes_and_rejects_html():
    halt = json.dumps([{"Code": "2314", "Name": "台揚"}], ensure_ascii=False).encode("utf-8")
    html = b"<html>FOR SECURITY REASONS</html>"
    urls = {u: n for n, u, _f, _d in ra.SOURCES}
    pay = {u: b"[]" for u in urls}
    pay[ra._TWSE + "exchangeReport/TWTAWU"] = halt
    pay[ra._TWSE + "announcement/punish"] = html                       # 被擋：回 HTML
    pay["https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"] = "資料日期,證券代號,持股分級\n20261106,2330,1\n".encode("utf-8")
    d1 = datetime(2031, 6, 2, 18, 40, tzinfo=TPE)
    r1 = ra.snapshot(now=d1, fetch=_fake(pay))
    assert r1["sources"]["twse_halt"] == "saved:1" and r1["sources"]["tdcc_holding_dist"] == "saved:1"
    assert r1["sources"]["twse_disposal"] == "error" and r1["errors"] >= 1
    sid = next(x["id"] for x in ra.index() if x["source"] == "twse_halt" and x["snap_date"] == "2031-06-02")
    body, src = ra.content(sid)
    assert body == halt and src == "twse_halt"                          # 與來源位元組完全相同
    err = next(x for x in ra.index() if x["source"] == "twse_disposal" and x["snap_date"] == "2031-06-02")
    assert err["status"] == "error" and ra.content(err["id"]) == (None, None)
    r2 = ra.snapshot(now=d1 + timedelta(days=1), fetch=_fake(pay))     # 內容沒變：不重存
    assert r2["sources"]["twse_halt"] == "unchanged" and r2["sources"]["tdcc_holding_dist"] == "unchanged"
    assert ra.done_today(d1 + timedelta(days=1))


def test_public_endpoints_list_and_serve_raw():
    import app as app_mod
    halt = b'[{"Code":"9999"}]'
    pay = {u: b"[]" for _n, u, _f, _d in ra.SOURCES}
    pay[ra._TWSE + "exchangeReport/TWTAWU"] = halt
    pay["https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"] = b"a,b\n1,2\n"
    ra.snapshot(now=datetime(2031, 7, 1, 18, 40, tzinfo=TPE), fetch=_fake(pay))
    c = app_mod.app.test_client()
    j = c.get("/api/public/research-snapshots").get_json()
    assert j["ok"] and "content_gz_b64" not in j["snapshots"][0]
    sid = next(x["id"] for x in j["snapshots"] if x["source"] == "twse_halt" and x["snap_date"] == "2031-07-01")
    r = c.get(f"/api/public/research-snapshots/{sid}")
    assert r.status_code == 200 and r.data == halt
    assert c.get("/api/public/research-snapshots/99999999").status_code == 404
