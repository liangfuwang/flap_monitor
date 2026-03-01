# Flap Monitor

Flap.sh Gift Vault 认领状态监控工具。监控 BSC 链上 Flap.sh 平台 Gift Vault 的状态变化，通过 Telegram 实时告警。

## 功能

- **链上状态监控** — 轮询 BSC 链上 Gift Vault 合约，检测状态变化（未认领 → 已认领 → 雪球回购）
- **Telegram 告警** — 状态变化时自动推送告警到指定 Telegram 群组
- **Web 管理面板** — 提供 Web 界面（端口 5555），支持动态添加/删除监控的代币合约地址
- **自动发现** — 未指定监控代币时，自动扫描链上事件发现新创建的 Gift Vault
- **RPC 故障转移** — 支持多个 BSC RPC 节点自动切换

## 金库状态

| 状态码 | 名称 | 说明 |
|--------|------|------|
| 0 | ACCUMULATING | 未认领，正在积累 |
| 1 | STREAMING | 已被认领 |
| 2 | SNOWBALL | 超时未认领，进入自动回购销毁 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的配置
```

需要配置：
- **Telegram API** — 从 https://my.telegram.org 获取 `API_ID` 和 `API_HASH`
- **BSC RPC** — 推荐使用 ANKR 或 NodeReal 的付费节点以获得更好的稳定性
- **监控代币** — 要监控的代币合约地址，逗号分隔

### 3. 启动

```bash
python flap_monitor.py
```

启动后访问 http://localhost:5555 打开 Web 管理面板。

## 技术栈

- **web3.py** — BSC 链上交互
- **Telethon** — Telegram MTProto 客户端
- **Flask** — Web 管理面板

## License

MIT
