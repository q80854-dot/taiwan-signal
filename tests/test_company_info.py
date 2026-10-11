"""個股研究延伸資料（2026-10-11）：公司概況、外資持股比率、集保股權分散的解析。
以假的 HTTP 回應驗證欄位關鍵字比對與大戶／散戶級距彙總（不打實際官方端點）。"""
import company_info as C


class FakeResp:
    def __init__(self, data=None, ok=True, content=None):
        self._d = data
        self.ok = ok
        self.status_code = 200 if ok else 500
        self.content = content

    def json(self):
        return self._d


def _patch(monkeypatch, fn):
    C._cache.clear()
    monkeypatch.setattr(C.requests, "get", fn)


def test_profile_parsing(monkeypatch):
    tw = [{"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24",
           "成立日期": "1987/02/21", "上市日期": "1994/09/05",
           "實收資本額": "259303804580", "董事長": "魏哲家", "網址": "https://www.tsmc.com"}]

    def fake(url, *a, **k):
        if "t187ap03_L" in url:
            return FakeResp(tw)
        return FakeResp([])
    _patch(monkeypatch, fake)
    p = C.get_profile("2330")
    assert p and p["name"] == "台積電" and p["chairman"] == "魏哲家"
    assert p["capital"] == 259303804580.0 and p["market"] == "TSE"
    assert C.get_profile("9999") is None


def test_foreign_holding_parsing(monkeypatch):
    # TWSE 報表格式：fields 表頭 + data 列陣列（與正式站一致）
    report = {
        "stat": "OK", "date": "20261010",
        "fields": ["證券代號", "證券名稱", "發行股數", "外資及陸資尚可投資股數",
                   "全體外資及陸資持有股數", "外資及陸資尚可投資比率",
                   "全體外資及陸資持股比率", "法令投資上限比率"],
        "data": [["2330", "台積電", "25930380458", "7000000000",
                  "18800000000", "27.00", "72.51", "100.00"]],
    }

    def fake(url, *a, **k):
        if "MI_QFIIS" in url:
            return FakeResp(report)
        return FakeResp({"stat": "", "data": []})
    _patch(monkeypatch, fake)
    f = C.get_foreign_holding("2330")
    assert f and f["ratio"] == 72.51 and f["ceiling"] == 100.0
    assert f["date"] == "20261010"
    # 確認挑到的是「持股比率」而非「尚可投資比率」(27.00)
    assert f["ratio"] != 27.00


def test_board_holding_sum_and_ratio(monkeypatch):
    # 董事+監察人分開欄位、無現成成數 → 由 (董事+監察人)/發行股數 計算比率
    rows = [{"公司代號": "2330", "發行股數": "1000000000",
             "全體董事持股": "120000000", "全體監察人持股": "30000000",
             "董監設質比率": "5.0"}]

    def fake(url, *a, **k):
        if "t187ap11_L" in url:
            return FakeResp(rows)
        return FakeResp([])
    _patch(monkeypatch, fake)
    b = C.get_board_holding("2330")
    assert b and b["shares"] == 150000000.0
    assert b["ratio"] == 15.0          # 150M / 1000M
    assert b["pledge_pct"] == 5.0
    assert C.get_board_holding("0001") is None


def test_dispersion_big_small(monkeypatch):
    header = "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例\n"
    rows = [("1", "5"), ("2", "5"), ("3", "3"),        # 散戶 = 13
            ("12", "7"), ("13", "8"), ("14", "10"), ("15", "20"),  # 大戶 = 45
            ("17", "100")]
    csv_text = header + "".join(f"20261010,2330,{lvl},10,0,{pct}\n" for lvl, pct in rows)

    def fake(url, *a, **k):
        if "tdcc" in url:
            return FakeResp(content=csv_text.encode("utf-8"))
        return FakeResp([])
    _patch(monkeypatch, fake)
    d = C.get_dispersion("2330")
    assert d and d["big_pct"] == 45.0 and d["small_pct"] == 13.0
    assert d["date"] == "20261010"
    assert C.get_dispersion("0000") is None
