# Polance

`Polance` 是一个用于查看多个 Polymarket 地址资产情况的本地网页监控工具。程序会定时拉取地址仓位、估值和 Polygon 链上的 USDC.e 余额，并提供适合手机访问的单页监控界面。

## 功能特点

- 支持同时监控多个 Polymarket 地址
- 使用 `.env` 管理地址、端口、RPC 和其他运行配置
- 自动轮询刷新，并支持手动立即刷新
- 展示持仓数量、可赎回数量、仓位估值、USDC.e 余额和总资产估值
- 前端为移动端友好的单页界面，适合 iPhone 浏览

## 运行环境

- Python 3.10 及以上

## 快速开始

1. 创建虚拟环境：

```bash
python3 -m venv .venv
```

2. 激活虚拟环境：

```bash
source .venv/bin/activate
```

3. 安装依赖：

```bash
pip install -r requirements.txt
```

4. 复制环境变量模板并填写你的地址：

```bash
cp .env.example .env
```

5. 启动程序：

```bash
python Polance.py
```

6. 打开浏览器访问：

- 本机访问：`http://127.0.0.1:8000`

## 环境变量说明

| 变量名 | 说明 |
| --- | --- |
| `HOST` | 服务监听地址，默认 `127.0.0.1` |
| `PORT` | Web 服务端口，默认 `8000` |
| `REFRESH_SECONDS` | 自动刷新间隔秒数，默认 `10` |
| `HIDE_DUST_THRESHOLD` | 隐藏小仓位阈值，默认 `1` |
| `HTTP_TIMEOUT` | HTTP 请求超时时间，默认 `12` 秒 |
| `POLYGON_RPC_URLS` | Polygon RPC 列表，多个地址用英文逗号分隔 |
| `ADDRESSES_JSON` | 监控地址 JSON 数组，支持 `name` 和 `address` 字段 |
| `DATA_API_BASE` | Polymarket 数据接口基础地址 |
| `USDC_E_CONTRACT` | Polygon 上 USDC.e 合约地址 |

## `ADDRESSES_JSON` 示例

```json
[
  {
    "name": "主号",
    "address": "0x1111111111111111111111111111111111111111"
  },
  {
    "name": "副号",
    "address": "0x2222222222222222222222222222222222222222"
  }
]
```

在 `.env` 中需要写成单行：

```env
ADDRESSES_JSON=[{"name":"主号","address":"0x1111111111111111111111111111111111111111"},{"name":"副号","address":"0x2222222222222222222222222222222222222222"}]
```

## 启动失败排查

- 如果提示 `ADDRESSES_JSON` 未配置，请检查 `.env` 是否存在且 JSON 格式正确
- 如果提示缺少 `dotenv`，请确认已经激活虚拟环境并执行 `pip install -r requirements.txt`
- 如果页面无法显示 USDC.e 余额，通常是当前 RPC 节点请求失败，可在 `.env` 中调整 `POLYGON_RPC_URLS`

## License

本项目采用 [MIT License](LICENSE) 开源许可。
