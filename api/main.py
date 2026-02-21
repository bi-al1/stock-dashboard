"""
投資管理Webアプリ - FastAPIバックエンド

エンドポイント一覧:
  GET  /api/watchlist
  POST /api/watchlist
  DELETE /api/watchlist/{code}
  POST /api/watchlist/status
  POST /api/watchlist/update-per
  GET  /api/portfolio
  POST /api/portfolio/buy
  POST /api/portfolio/sell
  GET  /api/healthcheck
  GET  /api/stocks/{code}           ← yfinanceリアルタイムデータ
  GET  /api/stocks/{code}/data      ← 分析JSONデータ（detail.html用）
  GET  /api/manifest
  DELETE /api/report/{code}
"""

import base64
import json
import os
import urllib.request
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ── パス設定 ──────────────────────────────────────────────
# このリポジトリ（kabumart-web）のルート
BASE_DIR = Path(__file__).parent.parent

FRONTEND_DIR = BASE_DIR / "frontend"

# ── yfinance ──────────────────────────────────────────────
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# ── FastAPI初期化 ─────────────────────────────────────────
app = FastAPI(title="投資管理API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Vercelのドメインに後で絞る
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── GitHub API 設定 ────────────────────────────────────────
GITHUB_OWNER  = "bi-al1"

# 全データは stok-analyzer リポジトリで一元管理
# stock-dashboard リポジトリにはコードのみ（データなし）
GITHUB_REPO_ANALYZER   = "stok-analyzer"
GITHUB_BRANCH_ANALYZER = "master"

# GitHub上の各JSONのパス（stok-analyzer リポジトリルートからの相対パス）
GH_WATCHLIST_PATH = "watchlist/data/watchlist.json"
GH_PORTFOLIO_PATH = "portfolio-health/data/portfolio.json"
GH_MANIFEST_PATH  = "webapp/manifest.json"
GH_STOCKS_DIR     = "webapp/data/stocks"


# ── GitHub API ユーティリティ ──────────────────────────────
def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

def github_fetch_json(rel_path: str, repo: str = None, branch: str = None) -> dict:
    """GitHub Contents API からJSONを取得して dict で返す。ファイルが存在しない場合は FileNotFoundError を投げる。"""
    repo   = repo   or GITHUB_REPO_ANALYZER
    branch = branch or GITHUB_BRANCH_ANALYZER
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN が環境変数に設定されていません")
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{repo}/contents/{rel_path}?ref={branch}"
    req = urllib.request.Request(api_url, headers=_gh_headers(token))
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(f"GitHub上にファイルが存在しません: {rel_path}")
        raise RuntimeError(f"GitHub API GET 失敗: {e.code} {e.reason}")
    return json.loads(base64.b64decode(result["content"]).decode("utf-8"))

def github_update_json(rel_path: str, data: dict, message: str, repo: str = None, branch: str = None):
    """
    dict を JSON に変換してGitHub Contents API でコミットする。
    1. GET で SHA 取得
    2. PUT で新しい内容をコミット
    """
    repo   = repo   or GITHUB_REPO_ANALYZER
    branch = branch or GITHUB_BRANCH_ANALYZER
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN が環境変数に設定されていません")

    data["updated_at"] = datetime.now().isoformat()
    new_content_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{repo}/contents/{rel_path}"
    headers = _gh_headers(token)

    # Step1: SHA 取得（ファイルが存在しない場合は新規作成扱い）
    req = urllib.request.Request(api_url, headers=headers)
    sha = None
    try:
        with urllib.request.urlopen(req) as resp:
            current = json.loads(resp.read())
        sha = current["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise RuntimeError(f"GitHub API GET 失敗: {e.code} {e.reason}")
        # 404 = ファイル未存在 → sha なしで新規作成

    # Step2: PUT（sha があれば更新、なければ新規作成）
    put_payload = {
        "message": message,
        "content": base64.b64encode(new_content_bytes).decode(),
        "branch": branch,
        "committer": {"name": "Render Bot", "email": "render-bot@kabumart"},
    }
    if sha:
        put_payload["sha"] = sha
    body = json.dumps(put_payload).encode()
    req = urllib.request.Request(api_url, data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API PUT 失敗: {e.code} {e.reason}")

def github_delete_file(rel_path: str, message: str, repo: str = None, branch: str = None):
    """GitHub Contents API でファイルを削除する。"""
    repo   = repo   or GITHUB_REPO_ANALYZER
    branch = branch or GITHUB_BRANCH_ANALYZER
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN が環境変数に設定されていません")

    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{repo}/contents/{rel_path}"
    headers = _gh_headers(token)

    # SHA 取得
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            current = json.loads(resp.read())
        sha = current["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(rel_path)
        raise RuntimeError(f"GitHub API GET 失敗: {e.code} {e.reason}")

    # DELETE
    body = json.dumps({
        "message": message,
        "sha": sha,
        "branch": branch,
        "committer": {"name": "Render Bot", "email": "render-bot@kabumart"},
    }).encode()
    req = urllib.request.Request(api_url, data=body, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API DELETE 失敗: {e.code} {e.reason}")


# ── yfinance ユーティリティ ────────────────────────────────
def get_current_price(code: str) -> Optional[float]:
    if not YFINANCE_AVAILABLE:
        return None
    try:
        ticker = yf.Ticker(f"{code}.T")
        price = ticker.info.get("currentPrice") or ticker.info.get("regularMarketPrice")
        return float(price) if price else None
    except Exception:
        return None

def get_full_data(code: str) -> dict:
    if not YFINANCE_AVAILABLE:
        return {"error": "yfinance未インストール"}
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info

        hist = ticker.history(period="1y")
        rsi = sma50 = sma200 = golden_cross = death_cross = None

        if not hist.empty and len(hist) >= 14:
            closes = hist["Close"]
            delta = closes.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss
            rsi_series = 100 - (100 / (1 + rs))
            rsi = round(float(rsi_series.iloc[-1]), 1) if not rsi_series.empty else None

            if len(closes) >= 50:
                sma50 = float(closes.rolling(50).mean().iloc[-1])
            if len(closes) >= 200:
                sma200 = float(closes.rolling(200).mean().iloc[-1])
            if sma50 and sma200:
                golden_cross = sma50 > sma200
                death_cross  = sma50 < sma200

        return {
            "code": code,
            "price": {
                "current":  info.get("currentPrice") or info.get("regularMarketPrice"),
                "52w_high": info.get("fiftyTwoWeekHigh"),
                "52w_low":  info.get("fiftyTwoWeekLow"),
            },
            "technical": {
                "rsi":          rsi,
                "sma50":        round(sma50,  1) if sma50  else None,
                "sma200":       round(sma200, 1) if sma200 else None,
                "golden_cross": golden_cross,
                "death_cross":  death_cross,
            },
            "fundamentals": {
                "roe":              info.get("returnOnEquity"),
                "operating_margin": info.get("operatingMargins"),
                "revenue_growth":   info.get("revenueGrowth"),
            },
        }
    except Exception as e:
        return {"error": str(e)}

def health_alert(data: dict) -> dict:
    tech = data.get("technical", {})
    fund = data.get("fundamentals", {})

    rsi         = tech.get("rsi")
    death_cross = tech.get("death_cross")
    sma50       = tech.get("sma50")
    sma200      = tech.get("sma200")
    roe         = fund.get("roe")
    rev_growth  = fund.get("revenue_growth")
    op_margin   = fund.get("operating_margin")

    fund_bad = sum([
        roe        is not None and roe        < 0,
        rev_growth is not None and rev_growth < -0.1,
        op_margin  is not None and op_margin  < 0,
    ])

    sma_gap_pct = None
    if sma50 and sma200:
        sma_gap_pct = abs(sma50 - sma200) / sma200 * 100

    if death_cross and fund_bad >= 2:
        return {"level": "red",    "label": "🔴 撤退検討", "reason": "デッドクロス発生 + 業績複数悪化"}
    if sma_gap_pct is not None and sma_gap_pct <= 5 and fund_bad >= 1:
        return {"level": "orange", "label": "🟠 注意",     "reason": "SMA50とSMA200が接近 + 業績に陰り"}
    if (rsi is not None and rsi <= 30) or (sma50 and data.get("price", {}).get("current") and data["price"]["current"] < sma50):
        return {"level": "yellow", "label": "🟡 早期警告", "reason": "RSI売られすぎ or SMA50割れ"}
    return {"level": "green", "label": "✅ 問題なし", "reason": "特に懸念なし"}


# ── ウォッチリスト ─────────────────────────────────────────
class WatchlistAddRequest(BaseModel):
    code: str
    name: str
    note: str = ""
    kabumart_rank: str = ""
    per: Optional[float] = None

class WatchlistStatusRequest(BaseModel):
    code: str
    status: str  # "watching" | "interested" | "pending"

@app.get("/api/watchlist")
def get_watchlist():
    try:
        return github_fetch_json(GH_WATCHLIST_PATH)
    except FileNotFoundError:
        return {"watchlist": [], "updated_at": datetime.now().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/watchlist")
def add_watchlist(req: WatchlistAddRequest):
    try:
        data = github_fetch_json(GH_WATCHLIST_PATH)
    except FileNotFoundError:
        data = {"watchlist": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    for item in data["watchlist"]:
        if item["code"] == req.code:
            raise HTTPException(status_code=409, detail=f"{req.name}（{req.code}）はすでに登録済みです")

    entry = {
        "code": req.code,
        "name": req.name,
        "added_date": datetime.now().strftime("%Y-%m-%d"),
        "note": req.note,
        "kabumart_rank": req.kabumart_rank,
        "status": "archived",
    }
    if req.per is not None:
        entry["per"] = req.per
        entry["per_history"] = [{"date": datetime.now().strftime("%Y-%m-%d"), "per": req.per, "source": "analysis"}]
    data["watchlist"].append(entry)
    try:
        github_update_json(GH_WATCHLIST_PATH, data, f"watchlist: {req.code} を追加")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "added", "count": len(data["watchlist"])}

@app.delete("/api/watchlist/{code}")
def delete_watchlist(code: str):
    try:
        data = github_fetch_json(GH_WATCHLIST_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    before = len(data["watchlist"])
    data["watchlist"] = [x for x in data["watchlist"] if x["code"] != code]
    if len(data["watchlist"]) == before:
        raise HTTPException(status_code=404, detail=f"{code} はウォッチリストに見つかりません")

    try:
        github_update_json(GH_WATCHLIST_PATH, data, f"watchlist: {code} を削除")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "status": "deleted", "count": len(data["watchlist"])}

@app.post("/api/watchlist/status")
def update_watchlist_status(req: WatchlistStatusRequest):
    VALID_STATUSES = {"watching", "interested", "archived"}
    if req.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"無効なステータスです: {req.status}")

    try:
        data = github_fetch_json(GH_WATCHLIST_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    entry = next((x for x in data["watchlist"] if x["code"] == req.code), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"{req.code} はウォッチリストに見つかりません")

    entry["status"] = req.status
    entry["updated_at"] = datetime.now().isoformat()
    try:
        github_update_json(GH_WATCHLIST_PATH, data, f"watchlist: {req.code} のステータスを {req.status} に変更")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "code": req.code, "status": req.status}

@app.post("/api/watchlist/update-per")
def update_watchlist_per():
    """ウォッチリスト銘柄の予想PERをyfinanceから一括更新する（archived以外）。"""
    if not YFINANCE_AVAILABLE:
        raise HTTPException(status_code=503, detail="yfinanceが利用できません")

    try:
        data = github_fetch_json(GH_WATCHLIST_PATH)
    except FileNotFoundError:
        return {"updated": 0, "results": [], "errors": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    errors = []

    for entry in data.get("watchlist", []):
        if entry.get("status") == "archived":
            continue

        code = entry["code"]
        try:
            ticker = yf.Ticker(f"{code}.T")
            info = ticker.info
            per = info.get("forwardPE")
            if per is None:
                per = info.get("trailingPE")
            if per is not None:
                per = round(float(per), 1)

            old_per = entry.get("per")
            entry["per"] = per

            history = entry.get("per_history", [])
            same_day = [h for h in history if h["date"] == today]
            if same_day:
                same_day[0]["per"] = per
                same_day[0]["source"] = "yfinance"
            else:
                history.append({"date": today, "per": per, "source": "yfinance"})
            entry["per_history"] = history

            results.append({
                "code": code,
                "name": entry.get("name", ""),
                "old_per": old_per,
                "new_per": per,
            })
        except Exception as e:
            errors.append({"code": code, "name": entry.get("name", ""), "error": str(e)})

    if results:
        try:
            github_update_json(
                GH_WATCHLIST_PATH, data,
                f"watchlist: {len(results)}銘柄のPERを一括更新",
            )
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=f"GitHub保存失敗: {e}")

    return {
        "updated": len(results),
        "results": results,
        "errors": errors,
        "checked_at": datetime.now().isoformat(),
    }


# ── ポートフォリオ ─────────────────────────────────────────
class BuyRequest(BaseModel):
    code: str
    name: str
    shares: int
    price: float
    note: str = ""

class SellRequest(BaseModel):
    code: str
    shares: int
    price: float

@app.get("/api/portfolio")
def get_portfolio():
    try:
        data = github_fetch_json(GH_PORTFOLIO_PATH)
    except FileNotFoundError:
        data = {"holdings": [], "trade_history": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    for h in data.get("holdings", []):
        current = get_current_price(h["code"])
        h["current_price"] = current
        if current:
            h["gain_loss"]     = round((current - h["avg_cost"]) * h["shares"], 0)
            h["gain_loss_pct"] = round((current - h["avg_cost"]) / h["avg_cost"] * 100, 1)
        else:
            h["gain_loss"] = h["gain_loss_pct"] = None
    return data

@app.post("/api/portfolio/buy")
def buy_stock(req: BuyRequest):
    try:
        data = github_fetch_json(GH_PORTFOLIO_PATH)
    except FileNotFoundError:
        data = {"holdings": [], "trade_history": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    trade = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "code": req.code, "name": req.name,
        "action": "buy", "shares": req.shares, "price": req.price,
    }
    data.setdefault("trade_history", []).append(trade)
    existing = next((h for h in data.get("holdings", []) if h["code"] == req.code), None)
    if existing:
        total_cost = existing["avg_cost"] * existing["shares"] + req.price * req.shares
        existing["shares"] += req.shares
        existing["avg_cost"] = round(total_cost / existing["shares"], 2)
    else:
        data.setdefault("holdings", []).append({
            "code": req.code, "name": req.name,
            "shares": req.shares, "avg_cost": req.price,
            "purchase_date": datetime.now().strftime("%Y-%m-%d"),
            "note": req.note,
        })
    try:
        github_update_json(GH_PORTFOLIO_PATH, data, f"portfolio: {req.name}（{req.code}）を{req.shares}株 買い記録")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "bought", "trade": trade}

@app.post("/api/portfolio/sell")
def sell_stock(req: SellRequest):
    try:
        data = github_fetch_json(GH_PORTFOLIO_PATH)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="ポートフォリオデータが存在しません")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    existing = next((h for h in data.get("holdings", []) if h["code"] == req.code), None)
    if not existing:
        raise HTTPException(status_code=404, detail=f"{req.code} はポートフォリオに見つかりません")
    if req.shares > existing["shares"]:
        raise HTTPException(status_code=400, detail=f"保有株数（{existing['shares']}株）を超える売却はできません")

    profit = round((req.price - existing["avg_cost"]) * req.shares, 0)
    trade = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "code": req.code, "name": existing["name"],
        "action": "sell", "shares": req.shares, "price": req.price, "profit": profit,
    }
    data.setdefault("trade_history", []).append(trade)
    existing["shares"] -= req.shares
    if existing["shares"] == 0:
        data["holdings"] = [h for h in data["holdings"] if h["code"] != req.code]

    try:
        github_update_json(GH_PORTFOLIO_PATH, data, f"portfolio: {existing['name']}（{req.code}）を{req.shares}株 売り記録")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "sold", "trade": trade}


@app.post("/api/portfolio/reset")
def reset_portfolio():
    """ポートフォリオデータを完全リセット（holdings・trade_historyを空にする）"""
    data = {"holdings": [], "trade_history": []}
    try:
        github_update_json(GH_PORTFOLIO_PATH, data, "portfolio: データをリセット")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "status": "reset"}

@app.delete("/api/portfolio/delete/{code}")
def delete_holding(code: str):
    """入力ミス等で保有銘柄をポートフォリオから完全削除（損益記録なし）"""
    try:
        data = github_fetch_json(GH_PORTFOLIO_PATH)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="ポートフォリオデータが存在しません")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    code_upper = code.upper()
    existing = next((h for h in data.get("holdings", []) if h["code"] == code_upper), None)
    if not existing:
        raise HTTPException(status_code=404, detail=f"{code_upper} はポートフォリオに見つかりません")

    data["holdings"] = [h for h in data["holdings"] if h["code"] != code_upper]
    try:
        github_update_json(GH_PORTFOLIO_PATH, data, f"portfolio: {existing['name']}（{code_upper}）を削除（入力ミス）")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "status": "deleted", "code": code_upper}

@app.delete("/api/portfolio/trade/{index}")
def delete_trade(index: int):
    """売買履歴から指定インデックスの取引を削除し、holdingsを再計算する。"""
    try:
        data = github_fetch_json(GH_PORTFOLIO_PATH)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="ポートフォリオデータが存在しません")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    history = data.get("trade_history", [])
    if index < 0 or index >= len(history):
        raise HTTPException(status_code=404, detail=f"取引インデックス {index} が範囲外です（全{len(history)}件）")

    deleted_trade = history.pop(index)

    # 残りの取引からholdingsを再構築
    holdings = {}
    for t in history:
        code = t["code"]
        if t["action"] == "buy":
            if code in holdings:
                h = holdings[code]
                total_cost = h["avg_cost"] * h["shares"] + t["price"] * t["shares"]
                h["shares"] += t["shares"]
                h["avg_cost"] = round(total_cost / h["shares"], 2)
            else:
                holdings[code] = {
                    "code": code, "name": t["name"],
                    "shares": t["shares"], "avg_cost": t["price"],
                    "purchase_date": t["date"], "note": "",
                }
        elif t["action"] == "sell":
            if code in holdings:
                holdings[code]["shares"] -= t["shares"]
                if holdings[code]["shares"] <= 0:
                    del holdings[code]

    data["holdings"] = list(holdings.values())
    data["trade_history"] = history

    desc = f"{deleted_trade['name']}（{deleted_trade['code']}）の{deleted_trade['action']}取引を削除"
    try:
        github_update_json(GH_PORTFOLIO_PATH, data, f"portfolio: {desc}")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "status": "deleted", "deleted": deleted_trade}



# ── ヘルスチェック ─────────────────────────────────────────
@app.get("/api/healthcheck")
def healthcheck():
    try:
        data = github_fetch_json(GH_PORTFOLIO_PATH)
    except FileNotFoundError:
        data = {"holdings": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    results = []
    for h in data.get("holdings", []):
        full  = get_full_data(h["code"])
        alert = health_alert(full)
        results.append({
            "code": h["code"], "name": h["name"],
            "shares": h["shares"], "avg_cost": h["avg_cost"],
            "current_price": (full.get("price") or {}).get("current"),
            "alert":        alert,
            "technical":    full.get("technical", {}),
            "fundamentals": full.get("fundamentals", {}),
        })
    summary = {lvl: sum(1 for r in results if r["alert"]["level"] == lvl) for lvl in ("green", "yellow", "orange", "red")}
    return {"summary": summary, "results": results, "checked_at": datetime.now().isoformat()}


# ── 個別銘柄データ ─────────────────────────────────────────
@app.get("/api/stocks/{code}/data")
def get_stock_data(code: str):
    """分析JSONデータを返す（detail.html 用）。"""
    try:
        return github_fetch_json(f"{GH_STOCKS_DIR}/{code.upper()}.json")
    except Exception:
        raise HTTPException(status_code=404, detail=f"{code} の分析データが見つかりません")

@app.get("/api/stocks/{code}")
def get_stock(code: str):
    data = get_full_data(code)
    if "error" in data:
        raise HTTPException(status_code=503, detail=data["error"])
    return data


# ── manifest ──────────────────────────────────────────────
@app.get("/api/manifest")
def get_manifest():
    try:
        return github_fetch_json(GH_MANIFEST_PATH)
    except FileNotFoundError:
        return {"stocks": [], "updated_at": datetime.now().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 分析レポート削除 ──────────────────────────────────────
@app.delete("/api/report/{code}")
def delete_report(code: str):
    """分析JSONをstok-analyzerリポジトリから削除し、manifest.jsonからも除外する。"""
    rel_path = f"{GH_STOCKS_DIR}/{code.upper()}.json"

    # Step1: 分析JSONを削除
    try:
        github_delete_file(rel_path, f"report: {code} の分析レポートを削除")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"{code} の分析レポートが見つかりません")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Step2: manifest.json から除外
    try:
        manifest = github_fetch_json(GH_MANIFEST_PATH)
        manifest["stocks"] = [s for s in manifest.get("stocks", []) if s.get("code") != code.upper()]
        github_update_json(GH_MANIFEST_PATH, manifest, f"manifest: {code} を除外")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"manifest更新失敗: {e}")

    return {"ok": True, "status": "deleted", "code": code}


# ── フロントエンド配信 ─────────────────────────────────────
@app.get("/")
def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "frontend not built yet"}

app.mount("/stocks", StaticFiles(directory=str(FRONTEND_DIR / "stocks")), name="stocks")
