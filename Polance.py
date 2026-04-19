#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket 多地址资产监控页（iOS 网页 App 重写版）

打开：
http://你的服务器IP:8000

本版重点：
1. 页面完全重写为 iOS 风格网页 App 观感
2. 前端采用 /api/snapshot 增量更新，不再整块替换 DOM
3. 滚动中、触摸中、惯性滚动未停稳时暂停刷新，减少 iPhone 小跳动
4. 顶部只保留 4 张总览卡片
5. 小仓位无条件隐藏，减少列表噪音

"""

import html
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()

# =========================================================
# 配置区：优先从 .env 读取，适合放到 GitHub
# 地址使用 JSON 数组，例如：
# ADDRESSES_JSON=[{"name":"主号","address":"0x..."},{"name":"副号","address":"0x..."}]
# =========================================================

DEFAULT_POLYGON_RPC_URLS = [
    "https://polygon.drpc.org",
    "https://tenderly.rpc.polygon.community",
    "https://polygon.publicnode.com",
    "https://1rpc.io/matic",
    "https://polygon.api.onfinality.io/public",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]
DEFAULT_DATA_API_BASE = "https://data-api.polymarket.com"
DEFAULT_USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name} 必须是整数，当前值：{value!r}") from exc


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name} 必须是数字，当前值：{value!r}") from exc


def load_addresses_from_env() -> List[Any]:
    raw = os.environ.get("ADDRESSES_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"环境变量 ADDRESSES_JSON 不是合法 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise SystemExit("环境变量 ADDRESSES_JSON 必须是 JSON 数组")
    return data


ADDRESSES = load_addresses_from_env()
HOST = env_str("HOST", "0.0.0.0")
PORT = env_int("PORT", 8000)
REFRESH_SECONDS = env_int("REFRESH_SECONDS", 10)
HIDE_DUST_THRESHOLD = env_float("HIDE_DUST_THRESHOLD", 1)
POLYGON_RPC_URLS_ENV = os.environ.get("POLYGON_RPC_URLS", "").strip()
POLYGON_RPC_URLS = [u.strip() for u in POLYGON_RPC_URLS_ENV.split(",") if u.strip()] or DEFAULT_POLYGON_RPC_URLS
RPC_RETRY_PER_ENDPOINT = env_int("RPC_RETRY_PER_ENDPOINT", 2)
RPC_RETRY_SLEEP_SECONDS = env_float("RPC_RETRY_SLEEP_SECONDS", 0.35)
MAX_RPC_ERROR_DISPLAY = env_int("MAX_RPC_ERROR_DISPLAY", 4)
DATA_API_BASE = env_str("DATA_API_BASE", DEFAULT_DATA_API_BASE)
USDC_E_CONTRACT = env_str("USDC_E_CONTRACT", DEFAULT_USDC_E_CONTRACT)
HTTP_TIMEOUT = env_int("HTTP_TIMEOUT", 12)

# =========================================================
# 工具函数
# =========================================================
def is_valid_address(addr: str) -> bool:
    if not isinstance(addr, str):
        return False
    if not addr.startswith("0x") or len(addr) != 42:
        return False
    try:
        int(addr[2:], 16)
        return True
    except Exception:
        return False


def q(value: Any, digits: int = 4) -> str:
    try:
        d = Decimal(str(value))
        return str(d.quantize(Decimal("1." + "0" * digits), rounding=ROUND_HALF_UP))
    except Exception:
        return str(value)


def q2(value: Any) -> str:
    try:
        d = Decimal(str(value))
        return f"{d.quantize(Decimal('1.00'), rounding=ROUND_HALF_UP)}"
    except Exception:
        return str(value)


def fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def shorten_text(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_http_error(exc: urllib.error.HTTPError) -> str:
    body = ""
    try:
        raw = exc.read()
        if raw:
            body = raw.decode("utf-8", errors="replace")
    except Exception:
        body = ""

    if body:
        return f"HTTP {exc.code}: {shorten_text(body)}"
    return f"HTTP {exc.code}: {exc.reason}"


def http_get_json(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "polymarket-balance-monitor/3.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise RuntimeError(format_http_error(e)) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}") from e
    except socket.timeout as e:
        raise RuntimeError("请求超时") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON 解析失败: {e}") from e


def http_post_json(url: str, payload: Dict[str, Any], timeout: int = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(
        normalize_url(url),
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "User-Agent": "polymarket-balance-monitor/3.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise RuntimeError(format_http_error(e)) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}") from e
    except socket.timeout as e:
        raise RuntimeError("请求超时") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON 解析失败: {e}") from e


def fetch_positions(address: str) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "user": address,
            "limit": 200,
            "offset": 0,
            "sortBy": "CURRENT",
            "sortDirection": "DESC",
            "sizeThreshold": 0,
        }
    )
    data = http_get_json(f"{DATA_API_BASE}/positions?{params}")
    if not isinstance(data, list):
        raise RuntimeError(f"/positions 返回异常：{data}")
    return data


def fetch_value(address: str) -> Decimal:
    data = http_get_json(f"{DATA_API_BASE}/value?{urllib.parse.urlencode({'user': address})}")
    if isinstance(data, list):
        if not data:
            return Decimal("0")
        return Decimal(str(data[0].get("value", 0)))
    if isinstance(data, dict):
        return Decimal(str(data.get("value", 0)))
    raise RuntimeError(f"/value 返回异常：{data}")


def build_erc20_balance_of_data(wallet: str) -> str:
    return "0x70a08231" + ("0" * 24) + wallet.lower().replace("0x", "")


def rpc_request(rpc_url: str, method: str, params: List[Any], timeout: int = HTTP_TIMEOUT) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    rpc_url = normalize_url(rpc_url)
    for attempt in range(1, RPC_RETRY_PER_ENDPOINT + 1):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": int(time.time() * 1000) % 1_000_000,
        }
        try:
            result = http_post_json(rpc_url, payload, timeout=timeout)
            if not isinstance(result, dict):
                raise RuntimeError(f"RPC 返回非对象：{result}")
            if result.get("error"):
                raise RuntimeError(shorten_text(json.dumps(result["error"], ensure_ascii=False)))
            return result
        except Exception as e:
            last_error = e
            if attempt < RPC_RETRY_PER_ENDPOINT:
                time.sleep(RPC_RETRY_SLEEP_SECONDS * attempt)
    raise RuntimeError(str(last_error) if last_error else "未知 RPC 错误")


def fetch_usdc_balance_via_rpc(rpc_url: str, wallet: str) -> Decimal:
    result = rpc_request(
        rpc_url,
        "eth_call",
        [{"to": USDC_E_CONTRACT, "data": build_erc20_balance_of_data(wallet)}, "latest"],
    )
    raw = result.get("result")
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise RuntimeError(f"RPC 未返回合法 result：{result}")
    amount = int(raw, 16)
    return Decimal(amount) / Decimal(10**6)


def fetch_usdc_balance_with_fallback(wallet: str, rpc_urls: List[str]) -> Tuple[Optional[Decimal], Optional[str], List[str]]:
    errors: List[str] = []
    for rpc in rpc_urls:
        rpc = normalize_url(rpc)
        if not rpc:
            continue
        try:
            bal = fetch_usdc_balance_via_rpc(rpc, wallet)
            return bal, rpc, errors
        except Exception as e:
            errors.append(f"{rpc} -> {e}")
    return None, None, errors


def normalize_accounts(items: List[Any]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    seen = set()
    for idx, item in enumerate(items, 1):
        if isinstance(item, str):
            address = item.strip()
            name = f"账户{idx}"
        elif isinstance(item, dict):
            address = str(item.get("address", "")).strip()
            name = str(item.get("name", "")).strip() or f"账户{idx}"
        else:
            raise SystemExit(f"ADDRESSES 第 {idx} 项格式不支持：{item!r}")
        if not is_valid_address(address):
            raise SystemExit(f"ADDRESSES 第 {idx} 项地址格式不正确：{address!r}")
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"name": name, "address": address})
    return normalized


def addr_short(addr: str) -> str:
    if len(addr) < 12:
        return addr
    return f"{addr[:8]}...{addr[-6:]}"


# =========================================================
# 数据结构
# =========================================================
@dataclass
class AddressSnapshot:
    address: str
    display_name: str = ""
    fetched_at: float = 0
    positions: List[Dict[str, Any]] = field(default_factory=list)
    positions_count: int = 0
    redeemable_count: int = 0
    position_value: Decimal = Decimal("0")
    usdc_balance: Optional[Decimal] = None
    total_estimated_value: Optional[Decimal] = None
    used_rpc: Optional[str] = None
    positions_error: Optional[str] = None
    value_error: Optional[str] = None
    rpc_error: Optional[str] = None
    rpc_error_list: List[str] = field(default_factory=list)


class MultiAddressMonitor:
    def __init__(self, accounts: List[Dict[str, str]], rpc_urls: List[str]):
        self.accounts = accounts
        self.addresses = [a["address"] for a in accounts]
        self.name_map = {a["address"]: a["name"] for a in accounts}
        self.rpc_urls = rpc_urls
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.snapshots: Dict[str, AddressSnapshot] = {
            a["address"]: AddressSnapshot(
                address=a["address"],
                display_name=a["name"],
                positions_error="初始化中...",
                value_error="初始化中...",
                rpc_error="初始化中...",
            )
            for a in accounts
        }

    def refresh_one(self, address: str) -> None:
        snap = AddressSnapshot(address=address, display_name=self.name_map.get(address, address), fetched_at=time.time())

        try:
            positions = fetch_positions(address)
            snap.positions = positions
            snap.positions_count = len(positions)
            snap.redeemable_count = sum(1 for p in positions if p.get("redeemable"))
        except Exception as e:
            snap.positions_error = str(e)

        try:
            snap.position_value = fetch_value(address)
        except Exception as e:
            snap.value_error = str(e)

        bal, used_rpc, rpc_errors = fetch_usdc_balance_with_fallback(address, self.rpc_urls)
        snap.usdc_balance = bal
        snap.used_rpc = used_rpc
        snap.rpc_error_list = rpc_errors
        if bal is None:
            snap.rpc_error = "；".join(rpc_errors[-MAX_RPC_ERROR_DISPLAY:]) if rpc_errors else "USDC.e 余额查询失败"

        if snap.usdc_balance is not None:
            snap.total_estimated_value = snap.usdc_balance + snap.position_value

        with self.lock:
            self.snapshots[address] = snap

    def refresh_all_once(self) -> None:
        with self.refresh_lock:
            for addr in self.addresses:
                self.refresh_one(addr)

    def loop(self) -> None:
        while True:
            self.refresh_all_once()
            time.sleep(REFRESH_SECONDS)

    def get_all(self) -> Dict[str, AddressSnapshot]:
        with self.lock:
            return dict(self.snapshots)


# =========================================================
# 页面
# =========================================================
def render_html() -> str:
    template = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="apple-mobile-web-app-title" content="Polymarket" />
  <meta name="mobile-web-app-capable" content="yes" />
  <meta name="theme-color" content="#eef3fb" />
  <meta name="format-detection" content="telephone=no,date=no,address=no,email=no" />
  <title>Polymarket 多地址资产监控</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3fb;
      --bg-2: #e7edf7;
      --surface: rgba(255,255,255,.78);
      --surface-strong: rgba(255,255,255,.92);
      --surface-soft: rgba(248,250,255,.86);
      --line: rgba(15,23,42,.08);
      --text: #121826;
      --muted: #6b768c;
      --blue: #3478f6;
      --green: #11a97e;
      --yellow: #cb8a0c;
      --red: #d3545c;
      --shadow: 0 10px 30px rgba(15,23,42,.08);
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-right: env(safe-area-inset-right, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
      --safe-left: env(safe-area-inset-left, 0px);
      --app-height: 100vh;
    }

    * {
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
    }

    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", sans-serif;
      -webkit-font-smoothing: antialiased;
    }

    body {
      position: fixed;
      inset: 0;
      width: 100%;
      height: var(--app-height);
      overflow: hidden;
      overscroll-behavior: none;
      -webkit-overflow-scrolling: touch;
      background:
        radial-gradient(circle at 0% 0%, rgba(52,120,246,.10), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(17,169,126,.06), transparent 25%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      z-index: -1;
      pointer-events: none;
      background:
        radial-gradient(circle at 0% 0%, rgba(52,120,246,.10), transparent 28%),
        radial-gradient(circle at 100% 10%, rgba(17,169,126,.06), transparent 25%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
    }

    .app {
      width: 100%;
      height: var(--app-height);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      padding: 0;
      position: relative;
    }

    .app::after {
      content: "";
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      height: calc(var(--safe-bottom) + 2px);
      pointer-events: none;
      background: linear-gradient(
        180deg,
        rgba(231,237,247,0) 0%,
        rgba(231,237,247,1) 100%
      );
      z-index: 2;
    }

    .chrome {
      position: relative;
      z-index: 30;
      flex: 0 0 auto;
      padding-top: calc(var(--safe-top) + 10px);
      padding-bottom: 10px;
      padding-left: max(14px, var(--safe-left));
      padding-right: max(14px, var(--safe-right));
    }

    .chrome::before {
      content: "";
      position: absolute;
      inset: 0 0 -48px 0;
      z-index: -1;
      pointer-events: none;
      background: linear-gradient(
        180deg,
        rgba(255,255,255,.40) 0%,
        rgba(255,255,255,.08) calc(100% - 48px),
        rgba(255,255,255,0) 100%
      );
      backdrop-filter: saturate(180%) blur(24px);
      -webkit-backdrop-filter: saturate(180%) blur(24px);
      -webkit-mask-image: linear-gradient(
        to bottom,
        black 0%,
        black calc(100% - 48px),
        transparent 100%
      );
      mask-image: linear-gradient(
        to bottom,
        black 0%,
        black calc(100% - 48px),
        transparent 100%
      );
    }

    .appbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 6px 2px 14px;
    }

    .titleblock {
      min-width: 0;
    }

    .appname {
      margin: 0;
      font-size: 28px;
      line-height: 1.05;
      letter-spacing: -0.035em;
      font-weight: 780;
    }

    .stamp {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .status-pill,
    .refresh-btn {
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.74);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      box-shadow: 0 6px 22px rgba(15,23,42,.05);
      font-size: 12px;
      color: var(--muted);
      height: 36px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 0 12px;
    }

    .refresh-btn {
      color: var(--text);
      font-weight: 700;
      cursor: pointer;
      appearance: none;
      border-radius: 999px;
    }

    .pressable {
      position: relative;
      overflow: hidden;
      transition: transform .12s ease, box-shadow .16s ease, background .18s ease, opacity .18s ease;
      transform-origin: center;
    }

    .pressable:active,
    .pressable.is-pressed {
      transform: scale(.965);
      box-shadow: 0 3px 12px rgba(15,23,42,.10);
    }

    .refresh-btn:active {
      transform: scale(.98);
    }

    .refresh-btn.is-loading {
      pointer-events: none;
      opacity: .88;
    }

    .refresh-btn.is-loading::after {
      content: '';
      width: 12px;
      height: 12px;
      border-radius: 50%;
      border: 2px solid rgba(18,24,38,.18);
      border-top-color: rgba(18,24,38,.72);
      animation: spin .7s linear infinite;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #2ec26f;
      box-shadow: 0 0 0 4px rgba(46,194,111,.14);
      flex: 0 0 auto;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }

    .card {
      border-radius: 22px;
      background: var(--surface);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      padding: 16px;
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
    }

    .metric-label {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 8px;
    }

    .metric-value {
      font-size: 25px;
      line-height: 1.08;
      font-weight: 760;
      letter-spacing: -0.03em;
    }

    .m-green { color: var(--green); }
    .m-blue { color: var(--blue); }
    .m-yellow { color: var(--yellow); }

    .nav-wrap {
      margin-top: 14px;
    }

    .address-nav {
      display: flex;
      gap: 10px;
      overflow-x: auto;
      padding: 2px 1px 4px;
      scrollbar-width: none;
      -ms-overflow-style: none;
    }

    .address-nav::-webkit-scrollbar {
      display: none;
    }

    .chip {
      flex: 0 0 auto;
      display: inline-flex;
      flex-direction: column;
      align-items: flex-start;
      justify-content: center;
      min-width: 112px;
      padding: 10px 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.76);
      color: var(--text);
      box-shadow: 0 4px 16px rgba(15,23,42,.04);
      appearance: none;
      cursor: pointer;
    }

    .chip-name {
      font-size: 13px;
      font-weight: 730;
      line-height: 1.2;
    }

    .chip-sub {
      margin-top: 4px;
      font-size: 11px;
      color: var(--muted);
    }

    .chip.active {
      background: rgba(52,120,246,.10);
      border-color: rgba(52,120,246,.16);
      box-shadow: 0 8px 20px rgba(52,120,246,.08);
    }

    .chip.is-pressed {
      background: rgba(52,120,246,.12);
    }

    .detail-scroll {
      position: relative;
      z-index: 1;
      flex: 1 1 auto;
      min-height: 0;
      overflow-y: auto;
      overflow-x: hidden;
      overscroll-behavior-y: contain;
      -webkit-overflow-scrolling: touch;
      scroll-behavior: auto;
      background: transparent;
      padding:
        48px
        max(14px, var(--safe-right))
        0
        max(14px, var(--safe-left));
    }

    .panel-list {
      padding-bottom: calc(var(--safe-bottom) + 8px);
      min-height: calc(100% + var(--safe-bottom));
    }

    .panel {
      border-radius: 26px;
      background: var(--surface-strong);
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      overflow: clip;
      scroll-margin-top: 28px;
    }

    .panel + .panel {
      margin-top: 18px;
    }

    .panel-head {
      padding: 18px;
      display: grid;
      gap: 14px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,.84), rgba(255,255,255,.72));
    }

    .wallet-name {
      font-size: 19px;
      line-height: 1.15;
      font-weight: 760;
      letter-spacing: -0.02em;
    }

    .wallet-sub {
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
      word-break: break-all;
    }

    .mini-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }

    .mini {
      border-radius: 16px;
      background: var(--surface-soft);
      border: 1px solid rgba(15,23,42,.06);
      padding: 12px;
    }

    .mini .k {
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 8px;
    }

    .mini .v {
      font-size: 18px;
      line-height: 1.08;
      font-weight: 740;
      letter-spacing: -0.02em;
      word-break: break-word;
    }

    .error {
      margin: 0 18px 14px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(211,84,92,.08);
      border: 1px solid rgba(211,84,92,.14);
      color: #9b3740;
      font-size: 13px;
      line-height: 1.55;
      word-break: break-word;
    }

    .list {
      padding: 12px 14px 14px;
      display: grid;
      gap: 12px;
    }

    .pos-card {
      border-radius: 18px;
      background: rgba(246,248,252,.92);
      border: 1px solid rgba(15,23,42,.06);
      padding: 14px;
    }

    .pos-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
    }

    .market-title {
      font-size: 15px;
      line-height: 1.42;
      font-weight: 700;
      color: #182033;
    }

    .outcome {
      flex: 0 0 auto;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(52,120,246,.10);
      color: #245fc6;
      font-size: 12px;
      font-weight: 700;
    }

    .slug {
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
      word-break: break-all;
    }

    .kv {
      margin-top: 12px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px 14px;
    }

    .item .k {
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 4px;
    }

    .item .v {
      font-size: 14px;
      line-height: 1.35;
      color: var(--text);
      word-break: break-word;
    }

    .pos {
      color: var(--green);
      font-weight: 700;
    }

    .neg {
      color: var(--red);
      font-weight: 700;
    }

    .empty {
      padding: 20px 14px;
      text-align: center;
      font-size: 13px;
      color: var(--muted);
    }

    .hidden-note {
      padding: 0 18px 14px;
      font-size: 12px;
      color: var(--yellow);
    }

    .panel.jump-focus {
      animation: jumpFocus 1.0s ease;
    }

    @keyframes jumpFocus {
      0% { transform: translateY(0); box-shadow: var(--shadow); }
      28% { transform: translateY(-3px); box-shadow: 0 16px 36px rgba(52,120,246,.16); }
      100% { transform: translateY(0); box-shadow: var(--shadow); }
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    @media (max-width: 860px) {
      .summary,
      .mini-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 680px) {
      .chrome {
        padding-left: max(12px, var(--safe-left));
        padding-right: max(12px, var(--safe-right));
      }

      .detail-scroll {
        padding-top: 48px;
        padding-left: max(12px, var(--safe-left));
        padding-right: max(12px, var(--safe-right));
      }

      .appbar {
        align-items: flex-start;
        flex-direction: column;
      }

      .actions {
        width: 100%;
        justify-content: space-between;
      }

      .summary {
        gap: 10px;
      }

      .card {
        border-radius: 20px;
        padding: 14px;
      }

      .metric-value {
        font-size: 22px;
      }

      .chip {
        min-width: 96px;
        border-radius: 16px;
      }

      .panel {
        border-radius: 22px;
      }

      .panel-head {
        padding: 16px;
      }

      .list {
        padding: 10px 12px 12px;
      }

      .pos-card {
        border-radius: 16px;
        padding: 13px;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <section class="chrome">
      <div class="appbar">
        <div class="titleblock">
          <h1 class="appname">Polymarket</h1>
          <div class="stamp">更新时间 <span id="last-refresh">初始化中</span> · 自动刷新 __REFRESH_TEXT__</div>
        </div>
        <div class="actions">
          <div class="status-pill"><span class="dot"></span><span id="status-text">监控中</span></div>
          <button class="refresh-btn pressable" id="force-refresh-btn" type="button">强制刷新</button>
        </div>
      </div>
      <section class="summary" id="summary-grid"></section>
      <section class="nav-wrap">
        <div class="address-nav" id="address-nav"></div>
      </section>
    </section>

    <section class="detail-scroll" id="detail-scroll">
      <main class="panel-list" id="panel-list"></main>
    </section>
  </div>

  <script>
    const REFRESH_MS = __REFRESH_MS__;
    const HIDE_DUST_THRESHOLD = __DUST__;
    const detailScroll = document.getElementById('detail-scroll');
    const refreshBtn = document.getElementById('force-refresh-btn');

    const appState = {
      timer: null,
      polling: false,
      touching: false,
      lastScrollAt: 0,
      refreshQueued: false,
      forceRefreshRequested: false,
      scrollAnimFrame: null,
      scrollAnimToken: 0,
      panelEls: new Map(),
      chipEls: new Map(),
    };

    function updateAppHeight() {
      const h = window.innerHeight;
      document.documentElement.style.setProperty('--app-height', h + 'px');
    }

    function fmtMoney(v) {
      const n = Number(v || 0);
      return Number.isFinite(n) ? n.toFixed(2) : '0.00';
    }

    function fmtNum(v, digits = 4) {
      const n = Number(v || 0);
      return Number.isFinite(n) ? n.toFixed(digits) : '0.' + '0'.repeat(digits);
    }

    function safeText(v) {
      return (v == null ? '' : String(v));
    }

    function shortAddr(addr) {
      return addr && addr.length > 14 ? addr.slice(0, 8) + '...' + addr.slice(-6) : (addr || '');
    }

    function tapFlash(el) {
      if (!el) return;
      el.classList.remove('is-pressed');
      void el.offsetWidth;
      el.classList.add('is-pressed');
      setTimeout(() => el.classList.remove('is-pressed'), 180);
    }

    function flashPanel(panel) {
      if (!panel) return;
      panel.classList.remove('jump-focus');
      void panel.offsetWidth;
      panel.classList.add('jump-focus');
      setTimeout(() => panel.classList.remove('jump-focus'), 1000);
    }

    function setRefreshButtonLoading(loading) {
      refreshBtn.classList.toggle('is-loading', !!loading);
      refreshBtn.textContent = loading ? '刷新中' : '强制刷新';
    }

    function canRefreshNow() {
      if (appState.forceRefreshRequested) return true;
      if (document.hidden) return false;
      if (appState.touching) return false;
      if (Date.now() - appState.lastScrollAt < 900) return false;
      return true;
    }

    function restartTimer() {
      if (appState.timer) clearInterval(appState.timer);
      appState.timer = setInterval(() => {
        if (canRefreshNow()) refreshData(false);
        else appState.refreshQueued = true;
      }, REFRESH_MS);
    }

    function maybeRunQueuedRefresh() {
      if (!appState.refreshQueued) return;
      if (!canRefreshNow()) return;
      appState.refreshQueued = false;
      refreshData(false);
    }

    function makeMetricCard(label, value, extraClass = '') {
      const el = document.createElement('article');
      el.className = 'card';
      if (extraClass) el.classList.add(extraClass);

      const l = document.createElement('div');
      l.className = 'metric-label';
      l.textContent = label;

      const v = document.createElement('div');
      v.className = 'metric-value';
      v.textContent = value;

      el.append(l, v);
      return el;
    }

    function renderSummary(data) {
      const root = document.getElementById('summary-grid');
      const totalAsset = data.reduce((s, x) => s + Number(x.total_estimated_value || 0), 0);
      const totalPos = data.reduce((s, x) => s + Number(x.position_value || 0), 0);
      const totalUsdc = data.reduce((s, x) => s + Number(x.usdc_balance || 0), 0);

      root.replaceChildren(
        makeMetricCard('已统计地址', String(data.length)),
        makeMetricCard('已知总资产', '$' + fmtMoney(totalAsset), 'm-green'),
        makeMetricCard('总持仓价值', '$' + fmtMoney(totalPos), 'm-yellow'),
        makeMetricCard('已知 USDC.e', '$' + fmtMoney(totalUsdc), 'm-blue')
      );
    }

    function ensureChip(snap) {
      if (appState.chipEls.has(snap.address)) return appState.chipEls.get(snap.address);

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip pressable';
      btn.dataset.address = snap.address;

      const name = document.createElement('div');
      name.className = 'chip-name';

      const sub = document.createElement('div');
      sub.className = 'chip-sub';

      btn.append(name, sub);
      btn.addEventListener('click', () => {
        tapFlash(btn);
        scrollToPanel(snap.address);
      });

      appState.chipEls.set(snap.address, btn);
      return btn;
    }

    function syncChips(data) {
      const root = document.getElementById('address-nav');
      const nodes = data.map(snap => {
        const chip = ensureChip(snap);
        chip.querySelector('.chip-name').textContent = safeText(snap.display_name || shortAddr(snap.address));
        chip.querySelector('.chip-sub').textContent = shortAddr(snap.address);
        return chip;
      });
      root.replaceChildren(...nodes);
      updateActiveChip();
    }

    function revealChip(address) {
      const chip = appState.chipEls.get(address);
      if (!chip) return;
      try {
        chip.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
      } catch (_) {
        chip.scrollIntoView();
      }
    }

    function getPanelTargetTop(panel) {
      const scrollerRect = detailScroll.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      const landingGap = Math.max(14, Math.min(36, Math.round(detailScroll.clientHeight * 0.04)));
      return Math.max(0, detailScroll.scrollTop + (panelRect.top - scrollerRect.top) - landingGap);
    }

    function updateActiveChip() {
      const panels = Array.from(document.querySelectorAll('.panel[data-address]'));
      if (!panels.length) return;

      const scrollerRect = detailScroll.getBoundingClientRect();
      const anchorY = scrollerRect.top + Math.max(18, Math.min(42, Math.round(detailScroll.clientHeight * 0.05)));

      let current = null;
      let nearest = null;
      let nearestDistance = Infinity;

      for (const panel of panels) {
        const rect = panel.getBoundingClientRect();
        const containsAnchor = rect.top <= anchorY && rect.bottom > anchorY;
        const distance = Math.abs(rect.top - anchorY);

        if (containsAnchor) {
          current = panel.dataset.address;
          break;
        }
        if (distance < nearestDistance) {
          nearestDistance = distance;
          nearest = panel.dataset.address;
        }
      }

      current = current || nearest || panels[0].dataset.address;
      appState.chipEls.forEach((chip, address) => chip.classList.toggle('active', address === current));
      revealChip(current);
    }

    function stopAnimatedScroll() {
      if (appState.scrollAnimFrame != null) {
        cancelAnimationFrame(appState.scrollAnimFrame);
        appState.scrollAnimFrame = null;
      }
      appState.scrollAnimToken += 1;
    }

    function easeInOutQuart(t) {
      return t < 0.5 ? 8 * t * t * t * t : 1 - Math.pow(-2 * t + 2, 4) / 2;
    }

    function animateDetailScroll(targetTop) {
      stopAnimatedScroll();

      const startTop = detailScroll.scrollTop;
      const maxTop = Math.max(0, detailScroll.scrollHeight - detailScroll.clientHeight);
      const finalTop = Math.max(0, Math.min(targetTop, maxTop));
      const distance = finalTop - startTop;

      if (Math.abs(distance) < 1) {
        detailScroll.scrollTop = finalTop;
        updateActiveChip();
        return;
      }

      const duration = Math.max(360, Math.min(900, 280 + Math.abs(distance) * 0.45));
      const startedAt = performance.now();
      const token = appState.scrollAnimToken;

      const step = (now) => {
        if (token !== appState.scrollAnimToken) return;
        const progress = Math.min(1, (now - startedAt) / duration);
        const eased = easeInOutQuart(progress);
        detailScroll.scrollTop = startTop + distance * eased;

        if (progress < 1) {
          appState.scrollAnimFrame = requestAnimationFrame(step);
        } else {
          detailScroll.scrollTop = finalTop;
          appState.scrollAnimFrame = null;
          updateActiveChip();
        }
      };

      appState.scrollAnimFrame = requestAnimationFrame(step);
    }

    function scrollToPanel(address) {
      const panel = appState.panelEls.get(address);
      if (!panel) return;

      const targetTop = getPanelTargetTop(panel);
      appState.chipEls.forEach((chip, key) => chip.classList.toggle('active', key === address));
      revealChip(address);
      flashPanel(panel);
      animateDetailScroll(targetTop);
    }

    function buildMini(label, value, colorClass = '') {
      const el = document.createElement('div');
      el.className = 'mini';

      const k = document.createElement('div');
      k.className = 'k';
      k.textContent = label;

      const v = document.createElement('div');
      v.className = 'v';
      if (colorClass) v.classList.add(colorClass);
      v.textContent = value;

      el.append(k, v);
      return el;
    }

    function buildKv(label, value, cls = '') {
      const item = document.createElement('div');
      item.className = 'item';

      const k = document.createElement('div');
      k.className = 'k';
      k.textContent = label;

      const v = document.createElement('div');
      v.className = 'v';
      if (cls) v.classList.add(cls);
      v.textContent = value;

      item.append(k, v);
      return item;
    }

    function filteredPositions(snap) {
      return (snap.positions || []).filter(p => Number(p.currentValue || 0) >= HIDE_DUST_THRESHOLD);
    }

    function buildPositionCard(p) {
      const card = document.createElement('article');
      card.className = 'pos-card';

      const pnl = Number(p.cashPnl || 0);
      const pnlClass = pnl >= 0 ? 'pos' : 'neg';

      const top = document.createElement('div');
      top.className = 'pos-top';

      const left = document.createElement('div');
      left.style.minWidth = '0';

      const mt = document.createElement('div');
      mt.className = 'market-title';
      mt.textContent = safeText(p.title || '');

      const sg = document.createElement('div');
      sg.className = 'slug';
      sg.textContent = safeText(p.slug || '');

      left.append(mt, sg);

      const badge = document.createElement('div');
      badge.className = 'outcome';
      badge.textContent = safeText(p.outcome || '-') || '-';

      top.append(left, badge);

      const kv = document.createElement('div');
      kv.className = 'kv';
      kv.append(
        buildKv('持有份额', fmtNum(p.size, 4)),
        buildKv('买入均价', fmtNum(p.avgPrice, 4)),
        buildKv('当前市价', fmtNum(p.curPrice, 4)),
        buildKv('当前价值', '$' + fmtMoney(p.currentValue)),
        buildKv('浮动盈亏', '$' + fmtMoney(p.cashPnl) + ' (' + fmtNum(p.percentPnl, 2) + '%)', pnlClass),
        buildKv('赎回状态', p.redeemable ? '可赎回' : '-'),
        buildKv('结束日期', safeText((p.endDate || '').split('T')[0] || '-'))
      );

      card.append(top, kv);
      return card;
    }

    function positionSignature(list) {
      return JSON.stringify(list.map(p => [
        p.slug || '',
        p.outcome || '',
        Number(p.size || 0),
        Number(p.avgPrice || 0),
        Number(p.curPrice || 0),
        Number(p.currentValue || 0),
        Number(p.cashPnl || 0),
        Number(p.percentPnl || 0),
        !!p.redeemable,
        safeText((p.endDate || '').split('T')[0] || '')
      ]));
    }

    function ensurePanel(address) {
      if (appState.panelEls.has(address)) return appState.panelEls.get(address);

      const panel = document.createElement('section');
      panel.className = 'panel';
      panel.dataset.address = address;

      const head = document.createElement('div');
      head.className = 'panel-head';

      const title = document.createElement('div');

      const walletName = document.createElement('div');
      walletName.className = 'wallet-name';

      const walletSub = document.createElement('div');
      walletSub.className = 'wallet-sub';

      title.append(walletName, walletSub);

      const mini = document.createElement('div');
      mini.className = 'mini-grid';

      head.append(title, mini);

      const error = document.createElement('div');
      error.className = 'error';
      error.hidden = true;

      const hidden = document.createElement('div');
      hidden.className = 'hidden-note';
      hidden.hidden = true;

      const list = document.createElement('div');
      list.className = 'list';

      panel.append(head, error, hidden, list);
      panel._refs = { walletName, walletSub, mini, error, hidden, list, signature: '' };

      appState.panelEls.set(address, panel);
      return panel;
    }

    function updatePanel(snap) {
      const panel = ensurePanel(snap.address);
      const refs = panel._refs;

      refs.walletName.textContent = safeText(snap.display_name || shortAddr(snap.address));
      refs.walletSub.textContent = shortAddr(snap.address) + ' · 刷新：' + safeText(snap.fetched_at_text || '-') + ' · ' + (snap.used_rpc ? 'USDC.e 正常' : 'USDC.e 读取失败');

      const totalText = snap.total_estimated_value != null ? ('$' + fmtMoney(snap.total_estimated_value)) : '未完整统计';
      const usdcText = snap.usdc_balance != null ? ('$' + fmtMoney(snap.usdc_balance)) : '失败';

      refs.mini.replaceChildren(
        buildMini('估算总资产', totalText, 'm-green'),
        buildMini('USDC.e', usdcText, 'm-blue'),
        buildMini('持仓价值', '$' + fmtMoney(snap.position_value), 'm-yellow'),
        buildMini('可赎回', String(snap.redeemable_count || 0))
      );

      const errors = [];
      if (snap.positions_error) errors.push('持仓接口异常：' + snap.positions_error);
      if (snap.value_error) errors.push('价值接口异常：' + snap.value_error);
      if (snap.rpc_error) errors.push('USDC.e 余额异常：' + snap.rpc_error);

      if (errors.length) {
        refs.error.hidden = false;
        refs.error.innerHTML = errors.map(x =>
          x.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
        ).join('<br>');
      } else {
        refs.error.hidden = true;
        refs.error.textContent = '';
      }

      const visiblePositions = filteredPositions(snap);
      const hiddenCount = Math.max(0, (snap.positions || []).length - visiblePositions.length);

      if (hiddenCount > 0) {
        refs.hidden.hidden = false;
        refs.hidden.textContent = '已隐藏 ' + hiddenCount + ' 个低于 $' + HIDE_DUST_THRESHOLD.toFixed(2) + ' 的小仓位';
      } else {
        refs.hidden.hidden = true;
        refs.hidden.textContent = '';
      }

      const sig = positionSignature(visiblePositions);

      if (refs.signature !== sig) {
        refs.signature = sig;

        if (!visiblePositions.length) {
          const empty = document.createElement('div');
          empty.className = 'empty';
          empty.textContent = hiddenCount > 0
            ? '当前所有持仓均低于阈值，已隐藏。'
            : '当前没有持仓，或该地址接口暂时未返回持仓。';
          refs.list.replaceChildren(empty);
        } else {
          refs.list.replaceChildren(...visiblePositions.map(buildPositionCard));
        }
      }

      return panel;
    }

    function syncPanels(data) {
      const root = document.getElementById('panel-list');
      const nodes = data.map(updatePanel);
      root.replaceChildren(...nodes);
      updateActiveChip();
    }

    async function refreshData(force = false) {
      if (appState.polling) return;
      if (!force && !canRefreshNow()) {
        appState.refreshQueued = true;
        return;
      }

      appState.polling = true;
      appState.forceRefreshRequested = force;
      document.getElementById('status-text').textContent = force ? '强制刷新中' : '更新中';
      setRefreshButtonLoading(force);

      try {
        const endpoint = force ? '/api/refresh' : '/api/snapshot';
        const resp = await fetch(endpoint + '?_=' + Date.now(), { cache: 'no-store' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);

        const data = await resp.json();
        data.forEach(x => {
          x.fetched_at_text = x.fetched_at
            ? new Date(x.fetched_at * 1000).toLocaleString('zh-CN', { hour12: false })
            : '-';
        });

        renderSummary(data);
        syncChips(data);
        syncPanels(data);

        const latest = data.reduce((m, x) => Math.max(m, Number(x.fetched_at || 0)), 0);
        document.getElementById('last-refresh').textContent = latest
          ? new Date(latest * 1000).toLocaleString('zh-CN', { hour12: false })
          : '-';

        document.getElementById('status-text').textContent = '监控中';
      } catch (err) {
        console.error('refresh failed', err);
        document.getElementById('status-text').textContent = '更新失败';
      } finally {
        appState.polling = false;
        appState.forceRefreshRequested = false;
        setRefreshButtonLoading(false);
      }
    }

    detailScroll.addEventListener('scroll', () => {
      appState.lastScrollAt = Date.now();
      updateActiveChip();
    }, { passive: true });

    detailScroll.addEventListener('touchstart', () => {
      appState.touching = true;
    }, { passive: true });

    detailScroll.addEventListener('touchend', () => {
      appState.touching = false;
      appState.lastScrollAt = Date.now();
      setTimeout(maybeRunQueuedRefresh, 950);
    }, { passive: true });

    detailScroll.addEventListener('touchcancel', () => {
      appState.touching = false;
      appState.lastScrollAt = Date.now();
    }, { passive: true });

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (appState.timer) clearInterval(appState.timer);
      } else {
        updateAppHeight();
        refreshData(false);
        restartTimer();
      }
    });

    window.addEventListener('resize', updateAppHeight);
    window.addEventListener('orientationchange', updateAppHeight);

    window.addEventListener('pageshow', () => {
      updateAppHeight();
      refreshData(false);
      restartTimer();
    });

    refreshBtn.addEventListener('click', () => {
      tapFlash(refreshBtn);
      refreshData(true);
    });

    updateAppHeight();
    refreshData(false);
    restartTimer();
  </script>
</body>
</html>
"""
    refresh_text = f'{REFRESH_SECONDS} 秒' if REFRESH_SECONDS < 60 else (f'{REFRESH_SECONDS // 60} 分钟' if REFRESH_SECONDS % 60 == 0 else f'{REFRESH_SECONDS} 秒')
    return template.replace('__REFRESH_MS__', str(REFRESH_SECONDS * 1000)).replace('__DUST__', str(HIDE_DUST_THRESHOLD)).replace('__REFRESH_TEXT__', refresh_text)


# =========================================================
# HTTP 服务与启动
# =========================================================
def make_handler(monitor: MultiAddressMonitor):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path in ("/", "/index.html"):
                body = render_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/snapshot":
                snaps = list(monitor.get_all().values())
                snaps.sort(key=lambda s: (s.total_estimated_value is None, -(float(s.total_estimated_value or 0))))
                data = []
                for s in snaps:
                    data.append(
                        {
                            "address": s.address,
                            "display_name": s.display_name,
                            "fetched_at": s.fetched_at,
                            "positions_count": s.positions_count,
                            "redeemable_count": s.redeemable_count,
                            "position_value": float(s.position_value),
                            "usdc_balance": None if s.usdc_balance is None else float(s.usdc_balance),
                            "total_estimated_value": None if s.total_estimated_value is None else float(s.total_estimated_value),
                            "used_rpc": s.used_rpc,
                            "positions_error": s.positions_error,
                            "value_error": s.value_error,
                            "rpc_error": s.rpc_error,
                            "rpc_error_list": s.rpc_error_list,
                            "positions": s.positions,
                        }
                    )
                body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/refresh":
                monitor.refresh_all_once()
                snaps = list(monitor.get_all().values())
                snaps.sort(key=lambda s: (s.total_estimated_value is None, -(float(s.total_estimated_value or 0))))
                data = []
                for s in snaps:
                    data.append(
                        {
                            "address": s.address,
                            "display_name": s.display_name,
                            "fetched_at": s.fetched_at,
                            "positions_count": s.positions_count,
                            "redeemable_count": s.redeemable_count,
                            "position_value": float(s.position_value),
                            "usdc_balance": None if s.usdc_balance is None else float(s.usdc_balance),
                            "total_estimated_value": None if s.total_estimated_value is None else float(s.total_estimated_value),
                            "used_rpc": s.used_rpc,
                            "positions_error": s.positions_error,
                            "value_error": s.value_error,
                            "rpc_error": s.rpc_error,
                            "rpc_error_list": s.rpc_error_list,
                            "positions": s.positions,
                        }
                    )
                body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

        def log_message(self, fmt, *args):
            return

    return Handler


def main():
    if not ADDRESSES:
        raise SystemExit(
            "请先在 .env 文件中配置至少一个钱包地址，例如：\n"
            'ADDRESSES_JSON=[{"name":"主号","address":"0x1234..."}]'
        )

    accounts = normalize_accounts(ADDRESSES)

    deduped = []
    seen = set()
    for item in accounts:
        addr = item["address"].strip()
        if addr.lower() not in seen:
            deduped.append(item)
            seen.add(addr.lower())

    monitor = MultiAddressMonitor(deduped, POLYGON_RPC_URLS)
    monitor.refresh_all_once()

    t = threading.Thread(target=monitor.loop, daemon=True)
    t.start()

    server = ThreadingHTTPServer((HOST, PORT), make_handler(monitor))

    print("=" * 76)
    print("Polymarket 多地址资产监控已启动")
    print(f"监听地址: {HOST}")
    print(f"端口: {PORT}")
    print("USDC.e 余额 RPC 顺序:")
    for i, rpc in enumerate(POLYGON_RPC_URLS, 1):
        print(f"  {i:02d}. {rpc}")
    print("监控地址:")
    for i, item in enumerate(deduped, 1):
        print(f"  {i:02d}. {item['name']} -> {item['address']}")
    print(f"本机访问: http://127.0.0.1:{PORT}")
    print("=" * 76)

    server.serve_forever()


if __name__ == "__main__":
    main()
