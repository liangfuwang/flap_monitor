"""
Flap 金库认领告警监控
监控 Flap.sh 上 Gift Vault 的认领状态，当发现认领时通过 Telegram API 发送告警。
支持 Web 页面动态添加/删除监控 CA。
"""

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient
from web3 import Web3
from flask import Flask, request, jsonify, render_template_string

# ============================================================
# 配置常量
# ============================================================

# BSC RPC 节点（从环境变量读取，支持多节点故障转移）
BSC_RPC_ANKR = os.getenv("BSC_RPC_ANKR", "https://bsc-dataseed.binance.org/")
BSC_RPC_NODEREAL = os.getenv("BSC_RPC_NODEREAL", "https://bsc-dataseed1.binance.org/")
BSC_RPC_FALLBACKS = [BSC_RPC_ANKR, BSC_RPC_NODEREAL]

VAULT_PORTAL_ADDRESS = "0x90497450f2a706f1951b5bdda52B4E5d16f34C06"
FLAPX_VAULT_FACTORY_ADDRESS = "0x025549F52B03cF36f9e1a337c02d3AA7Af66ab32"

VAULT_STATE_ACCUMULATING = 0
VAULT_STATE_STREAMING = 1
VAULT_STATE_SNOWBALL = 2

VAULT_STATE_NAMES = {
    VAULT_STATE_ACCUMULATING: "ACCUMULATING (未认领)",
    VAULT_STATE_STREAMING: "STREAMING (已认领)",
    VAULT_STATE_SNOWBALL: "SNOWBALL (雪球回购)",
}

VAULT_PORTAL_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "taxToken", "type": "address"}],
        "name": "tryGetVault",
        "outputs": [
            {"internalType": "bool", "name": "found", "type": "bool"},
            {
                "components": [
                    {"internalType": "address", "name": "vault", "type": "address"},
                    {"internalType": "address", "name": "vaultFactory", "type": "address"},
                    {"internalType": "string", "name": "description", "type": "string"},
                    {"internalType": "bool", "name": "isOfficial", "type": "bool"},
                    {"internalType": "uint8", "name": "riskLevel", "type": "uint8"},
                ],
                "internalType": "struct VaultPortal.VaultInfo",
                "name": "info",
                "type": "tuple",
            },
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

FLAPX_VAULT_ABI = [
    {
        "inputs": [],
        "name": "description",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "state",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "xHandle",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "taxToken",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "createdAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

VAULT_PORTAL_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "token", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "vault", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "creator", "type": "address"},
        ],
        "name": "NewTaxTokenWithVault",
        "type": "event",
    }
]

FLAPX_FACTORY_EVENTS_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "taxToken", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "receiver", "type": "address"},
            {"indexed": False, "internalType": "string", "name": "xHandle", "type": "string"},
        ],
        "name": "VaultManaged",
        "type": "event",
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "name",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ============================================================
# Telegram 告警模块
# ============================================================


class TelegramAlert:
    def __init__(self, api_id: int, api_hash: str, phone: str, target_group: int):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.target_group = target_group
        self.client = TelegramClient("flap_monitor", api_id, api_hash)
        self._loop = None

    async def start(self):
        await self.client.start(phone=self.phone)
        self._loop = asyncio.get_event_loop()
        me = await self.client.get_me()
        logger.info(f"Telegram 已登录: {me.first_name} ({me.id})")

    async def stop(self):
        await self.client.disconnect()

    async def send_message(self, text: str) -> bool:
        try:
            await self.client.send_message(
                self.target_group,
                text,
                parse_mode="html",
                link_preview=False,
            )
            logger.info("Telegram 消息发送成功")
            return True
        except Exception as e:
            logger.error(f"Telegram 消息发送失败: {e}")
            return False

    def send_message_threadsafe(self, text: str):
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.send_message(text), self._loop)

    async def send_vault_claimed_alert(
        self, token_symbol, token_address, vault_address, x_handle,
        new_state, bnb_balance, description, receiver=None,
    ):
        msg = (
            f"🚨 <b>Flap 金库认领告警</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 代币: <b>${token_symbol}</b>\n"
            f"📍 代币地址: <code>{token_address}</code>\n"
            f"🏦 金库地址: <code>{vault_address}</code>\n"
            f"🐦 Gift Giver: @{x_handle}\n"
            f"📊 新状态: <b>{new_state}</b>\n"
            f"💰 金库余额: <b>{bnb_balance} BNB</b>\n"
        )
        if receiver:
            msg += f"👤 认领者: <code>{receiver}</code>\n"
        msg += (
            f"📝 描述: {description}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🔗 <a href='https://flap.sh/bnb/{token_address}/taxinfo'>查看详情</a> | "
            f"<a href='https://bscscan.com/address/{vault_address}'>BSCScan</a>"
        )
        return await self.send_message(msg)

    async def send_new_vault_alert(
        self, token_symbol, token_address, vault_address, x_handle,
        bnb_balance, description,
    ):
        msg = (
            f"📢 <b>Flap 新金库发现</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 代币: <b>${token_symbol}</b>\n"
            f"📍 代币地址: <code>{token_address}</code>\n"
            f"🏦 金库地址: <code>{vault_address}</code>\n"
            f"🐦 Gift Giver: @{x_handle}\n"
            f"📊 状态: <b>ACCUMULATING (未认领)</b>\n"
            f"💰 金库余额: <b>{bnb_balance} BNB</b>\n"
            f"📝 描述: {description}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🔗 <a href='https://flap.sh/bnb/{token_address}/taxinfo'>查看详情</a> | "
            f"<a href='https://bscscan.com/address/{vault_address}'>BSCScan</a>"
        )
        return await self.send_message(msg)

    async def send_snowball_alert(
        self, token_symbol, token_address, vault_address, x_handle,
        bnb_balance, description,
    ):
        msg = (
            f"❄️ <b>Flap 金库进入雪球模式</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 代币: <b>${token_symbol}</b>\n"
            f"📍 代币地址: <code>{token_address}</code>\n"
            f"🏦 金库地址: <code>{vault_address}</code>\n"
            f"🐦 Gift Giver: @{x_handle}\n"
            f"📊 状态: <b>SNOWBALL (雪球回购)</b>\n"
            f"💰 金库余额: <b>{bnb_balance} BNB</b>\n"
            f"📝 描述: {description}\n"
            f"⚠️ 金库超时未认领，已进入自动回购销毁模式\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🔗 <a href='https://flap.sh/bnb/{token_address}/taxinfo'>查看详情</a> | "
            f"<a href='https://bscscan.com/address/{vault_address}'>BSCScan</a>"
        )
        return await self.send_message(msg)

    async def send_startup_message(self, token_count: int, token_details: list[dict] = None):
        msg = (
            f"✅ <b>Flap 金库监控已启动</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"正在监控 {token_count} 个代币的金库状态\n"
            f"当金库被认领时将发送告警\n"
        )
        if token_details:
            msg += f"━━━━━━━━━━━━━━━\n"
            for t in token_details:
                msg += (
                    f"📦 <b>${t['symbol']}</b> | {t['state']}\n"
                    f"   CA: <code>{t['address']}</code>\n"
                    f"   💰 {t['balance']} BNB\n"
                )
        return await self.send_message(msg)

    async def send_monitoring_removed(self, token_address: str, token_symbol: str):
        msg = (
            f"🗑 <b>移除监控</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"CA: <code>{token_address}</code>\n"
            f"📦 代币: <b>${token_symbol}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已停止监控该代币的金库状态"
        )
        return await self.send_message(msg)

    async def send_monitoring_started(self, token_address: str, token_symbol: str, state_name: str, bnb_balance: str):
        msg = (
            f"👁 <b>新增监控</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"CA: <code>{token_address}</code>\n"
            f"📦 代币: <b>${token_symbol}</b>\n"
            f"📊 当前状态: <b>{state_name}</b>\n"
            f"💰 金库余额: <b>{bnb_balance} BNB</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"金库状态监控中..."
        )
        return await self.send_message(msg)

    async def send_already_claimed(self, token_address: str, token_symbol: str, bnb_balance: str, receiver: str = None):
        receiver_text = f"<code>{receiver}</code>" if receiver else "未知"
        msg = (
            f"⚠️ <b>该金库已被认领</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"CA: <code>{token_address}</code>\n"
            f"📦 代币: <b>${token_symbol}</b>\n"
            f"📊 状态: <b>STREAMING (已认领)</b>\n"
            f"💰 金库余额: <b>{bnb_balance} BNB</b>\n"
            f"👤 认领者: {receiver_text}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"无需监控，未加入监控列表"
        )
        return await self.send_message(msg)


# ============================================================
# 金库监控核心逻辑
# ============================================================


@dataclass
class VaultStatus:
    token_address: str
    vault_address: str
    vault_factory: str
    state: int
    description: str
    x_handle: str
    bnb_balance: float
    token_symbol: str = ""
    receiver: Optional[str] = None
    last_checked: float = field(default_factory=time.time)

    @property
    def state_name(self) -> str:
        return VAULT_STATE_NAMES.get(self.state, f"UNKNOWN({self.state})")

    @property
    def is_claimed(self) -> bool:
        return self.state == VAULT_STATE_STREAMING

    @property
    def is_snowball(self) -> bool:
        return self.state == VAULT_STATE_SNOWBALL

    @property
    def is_accumulating(self) -> bool:
        return self.state == VAULT_STATE_ACCUMULATING


class VaultMonitor:
    def __init__(self, rpc_url: str):
        # 构建 RPC 列表：环境变量指定的优先，然后是内置的 ANKR / NodeReal
        self.rpc_urls = [rpc_url]
        for url in BSC_RPC_FALLBACKS:
            if url not in self.rpc_urls:
                self.rpc_urls.append(url)
        self._current_rpc_idx = 0
        self._init_web3(self.rpc_urls[0])

        self.vault_cache: dict[str, VaultStatus] = {}

    def _init_web3(self, rpc_url: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.portal = self.w3.eth.contract(
            address=Web3.to_checksum_address(VAULT_PORTAL_ADDRESS),
            abi=VAULT_PORTAL_ABI,
        )

    def _ensure_connected(self):
        """检查连接，如断开则自动切换到下一个 RPC"""
        if self.w3.is_connected():
            return
        for i in range(len(self.rpc_urls)):
            idx = (self._current_rpc_idx + 1 + i) % len(self.rpc_urls)
            url = self.rpc_urls[idx]
            try:
                self._init_web3(url)
                if self.w3.is_connected():
                    self._current_rpc_idx = idx
                    rpc_name = "ANKR" if "ankr" in url else ("NodeReal" if "nodereal" in url else url[:40])
                    logger.info(f"BSC RPC 已切换到: {rpc_name}")
                    return
            except Exception:
                continue
        raise ConnectionError("所有 BSC RPC 节点均不可用")

    def get_token_symbol(self, token_address: str) -> str:
        try:
            self._ensure_connected()
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI,
            )
            return token.functions.symbol().call()
        except Exception:
            return "UNKNOWN"

    def get_vault_info(self, token_address: str) -> Optional[VaultStatus]:
        try:
            self._ensure_connected()
            addr = Web3.to_checksum_address(token_address)
            found, info = self.portal.functions.tryGetVault(addr).call()

            if not found:
                logger.debug(f"代币 {token_address} 没有关联的 vault")
                return None

            vault_addr = info[0]
            vault_factory = info[1]
            description = info[2]

            if vault_factory.lower() != FLAPX_VAULT_FACTORY_ADDRESS.lower():
                logger.debug(f"代币 {token_address} 的 vault 不是 Gift Vault，跳过")
                return None

            vault = self.w3.eth.contract(
                address=Web3.to_checksum_address(vault_addr),
                abi=FLAPX_VAULT_ABI,
            )

            state = vault.functions.state().call()
            x_handle = vault.functions.xHandle().call()

            balance_wei = self.w3.eth.get_balance(Web3.to_checksum_address(vault_addr))
            bnb_balance = float(Web3.from_wei(balance_wei, "ether"))

            symbol = self.get_token_symbol(token_address)

            # 如果已认领，尝试获取认领者地址（优先用缓存）
            receiver = None
            if state == VAULT_STATE_STREAMING:
                cached = self.vault_cache.get(token_address.lower())
                if cached and cached.receiver:
                    receiver = cached.receiver
                else:
                    receiver = self._get_receiver(vault, vault_addr, token_address)

            return VaultStatus(
                token_address=token_address.lower(),
                vault_address=vault_addr,
                vault_factory=vault_factory,
                state=state,
                description=description,
                x_handle=x_handle,
                bnb_balance=bnb_balance,
                token_symbol=symbol,
                receiver=receiver,
            )

        except Exception as e:
            logger.error(f"查询 vault 信息失败 (token={token_address}): {e}")
            return None

    def _get_receiver(self, vault, vault_addr: str, token_address: str) -> Optional[str]:
        """获取认领者地址：从 vault 合约的认领事件日志中读取（分批查询，最多往前 200000 块）"""
        CLAIM_EVENT_TOPIC = "0x73e8025b99a249caf38d8c6c925e21ee793a9d61e89dcdcf34ed8639a3fffa15"
        BATCH_SIZE = 5000
        MAX_BATCHES = 40  # 5000 * 40 = 200000 块 ≈ 7天
        try:
            # 优先使用 NodeReal RPC 查事件（支持更大块范围）
            event_w3 = Web3(Web3.HTTPProvider(BSC_RPC_NODEREAL))
            current_block = event_w3.eth.block_number
            for i in range(MAX_BATCHES):
                to_block = current_block - i * BATCH_SIZE
                from_block = max(0, to_block - BATCH_SIZE)
                if from_block >= to_block:
                    break
                logs = event_w3.eth.get_logs({
                    "address": Web3.to_checksum_address(vault_addr),
                    "topics": [CLAIM_EVENT_TOPIC],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                })
                if logs:
                    data = logs[-1]["data"].hex()
                    if len(data) >= 128:
                        receiver = "0x" + data[88:128]
                        if receiver != "0x" + "0" * 40:
                            return Web3.to_checksum_address(receiver)
                    return None
        except Exception as e:
            logger.debug(f"查询认领事件失败 (vault={vault_addr}): {e}")

        return None

    def check_vault_changes(self, token_address: str) -> tuple[Optional[VaultStatus], Optional[str]]:
        current = self.get_vault_info(token_address)
        if current is None:
            return None, None

        token_key = token_address.lower()
        prev = self.vault_cache.get(token_key)

        if prev is None:
            self.vault_cache[token_key] = current
            if current.is_accumulating:
                logger.info(
                    f"发现新金库: {current.token_symbol} ({token_address}), "
                    f"状态: {current.state_name}, 余额: {current.bnb_balance:.4f} BNB"
                )
                return current, "new"
            elif current.is_claimed:
                logger.info(f"发现已认领金库: {current.token_symbol} ({token_address})")
                return current, "already_claimed"
            else:
                return current, None
        else:
            self.vault_cache[token_key] = current

            if prev.state != current.state:
                if current.is_claimed and prev.is_accumulating:
                    logger.warning(
                        f"🚨 金库被认领! {current.token_symbol} ({token_address}), "
                        f"余额: {current.bnb_balance:.4f} BNB"
                    )
                    return current, "claimed"
                elif current.is_snowball:
                    logger.warning(f"❄️ 金库进入雪球模式! {current.token_symbol} ({token_address})")
                    return current, "snowball"
                else:
                    logger.info(
                        f"金库状态变化: {prev.state_name} -> {current.state_name} "
                        f"({current.token_symbol})"
                    )
                    return current, "state_changed"

            return current, None

    def discover_new_vaults(self, from_block: int = None, to_block: int = None) -> list[str]:
        try:
            self._ensure_connected()
            if to_block is None:
                to_block = self.w3.eth.block_number
            if from_block is None:
                from_block = max(0, to_block - 2000)

            logs = self.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": Web3.to_checksum_address(VAULT_PORTAL_ADDRESS),
            })

            new_tokens = []
            for log in logs:
                if len(log["topics"]) >= 2:
                    token_addr = "0x" + log["topics"][1].hex()[-40:]
                    token_addr = Web3.to_checksum_address(token_addr).lower()
                    if token_addr not in self.vault_cache:
                        new_tokens.append(token_addr)

            return list(set(new_tokens))

        except Exception as e:
            logger.error(f"发现新 vault 失败: {e}")
            return []


# ============================================================
# 全局共享状态
# ============================================================

load_dotenv()

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flap_monitor.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("flap_monitor")

running = True
watch_tokens: list[str] = []
watch_tokens_lock = threading.Lock()
monitor: Optional[VaultMonitor] = None
alert: Optional[TelegramAlert] = None
event_loop: Optional[asyncio.AbstractEventLoop] = None

# 监控日志（供前端展示）
monitor_logs: list[dict] = []
MAX_LOGS = 200


def add_log(level: str, message: str):
    monitor_logs.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "message": message,
    })
    if len(monitor_logs) > MAX_LOGS:
        del monitor_logs[: len(monitor_logs) - MAX_LOGS]


# ============================================================
# Flask Web 界面
# ============================================================

app = Flask(__name__)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Flap 金库监控</title>
<link rel="icon" href="https://flap.sh/favicon.ico" type="image/x-icon">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0e17;
    color: #e0e6ed;
    min-height: 100vh;
  }
  .container { max-width: 960px; margin: 0 auto; padding: 24px 16px; }
  h1 {
    font-size: 24px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 24px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  h1 .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #22c55e;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  /* Telegram 横幅 */
  .tg-banner {
    background: linear-gradient(135deg, #1a2744 0%, #1e3a5f 100%);
    border: 1px solid #2563eb44;
    border-radius: 10px;
    padding: 12px 18px;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: #93c5fd;
  }
  .tg-icon { font-size: 18px; flex-shrink: 0; }
  .tg-link {
    margin-left: auto;
    color: #60a5fa;
    text-decoration: none;
    font-weight: 600;
    white-space: nowrap;
    padding: 6px 14px;
    background: #2563eb33;
    border-radius: 6px;
    transition: all 0.2s;
  }
  .tg-link:hover { background: #2563eb; color: #fff; }

  /* 输入区域 */
  .input-section {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .input-section label {
    display: block;
    font-size: 14px;
    color: #9ca3af;
    margin-bottom: 8px;
  }
  .input-row {
    display: flex;
    gap: 10px;
  }
  .input-row input {
    flex: 1;
    padding: 10px 14px;
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 8px;
    color: #fff;
    font-size: 14px;
    font-family: 'SF Mono', Monaco, monospace;
    outline: none;
    transition: border-color 0.2s;
  }
  .input-row input:focus { border-color: #3b82f6; }
  .input-row input::placeholder { color: #6b7280; }
  .btn {
    padding: 10px 20px;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }
  .btn-primary {
    background: #3b82f6;
    color: #fff;
  }
  .btn-primary:hover { background: #2563eb; }
  .btn-primary:disabled {
    background: #1e40af;
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-danger {
    background: transparent;
    color: #ef4444;
    border: 1px solid #ef4444;
    padding: 4px 12px;
    font-size: 12px;
  }
  .btn-danger:hover { background: #ef4444; color: #fff; }
  .msg {
    margin-top: 10px;
    font-size: 13px;
    min-height: 20px;
  }
  .msg.success { color: #22c55e; }
  .msg.error { color: #ef4444; }

  /* 监控列表 */
  .section-title {
    font-size: 16px;
    font-weight: 600;
    color: #fff;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .section-title .count {
    background: #1e40af;
    color: #93c5fd;
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 10px;
  }
  .token-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 24px;
  }
  .token-card {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 10px;
    padding: 14px 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    transition: border-color 0.2s;
  }
  .token-card:hover { border-color: #374151; }
  .token-info { flex: 1; min-width: 0; }
  .token-symbol {
    font-weight: 600;
    font-size: 15px;
    color: #fff;
  }
  .token-addr {
    font-size: 12px;
    color: #6b7280;
    font-family: 'SF Mono', Monaco, monospace;
    word-break: break-all;
  }
  .token-meta {
    display: flex;
    gap: 14px;
    margin-top: 6px;
    font-size: 12px;
    color: #9ca3af;
  }
  .state-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
  }
  .state-accumulating { background: #14532d; color: #4ade80; }
  .state-streaming { background: #7c2d12; color: #fb923c; }
  .state-snowball { background: #1e3a5f; color: #7dd3fc; }
  .token-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: 12px;
    flex-shrink: 0;
  }
  .empty-state {
    text-align: center;
    color: #6b7280;
    padding: 40px;
    font-size: 14px;
  }

  /* 日志区域 */
  .log-section {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 12px;
    padding: 16px;
    max-height: 350px;
    overflow-y: auto;
  }
  .log-section::-webkit-scrollbar { width: 6px; }
  .log-section::-webkit-scrollbar-track { background: transparent; }
  .log-section::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
  .log-line {
    font-size: 12px;
    font-family: 'SF Mono', Monaco, monospace;
    padding: 3px 0;
    line-height: 1.6;
    border-bottom: 1px solid #1f293744;
  }
  .log-line .log-time { color: #6b7280; }
  .log-line .log-info { color: #60a5fa; }
  .log-line .log-warn { color: #fbbf24; }
  .log-line .log-error { color: #ef4444; }
  .log-line .log-success { color: #22c55e; }

  .links { margin-top: 4px; }
  .links a {
    font-size: 12px;
    color: #60a5fa;
    text-decoration: none;
    margin-right: 12px;
  }
  .links a:hover { text-decoration: underline; }

  .loading-spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid #ffffff44;
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
  <h1><div class="dot"></div>Flap 金库监控面板</h1>

  <div class="tg-banner">
    <span class="tg-icon">✈️</span>
    <span>加入 Telegram 告警群组，实时接收金库状态变更通知</span>
    <a href="https://t.me/+Wo8IxoX-BnhhMzBl" target="_blank" class="tg-link">加入群组 →</a>
  </div>

  <div class="input-section">
    <label>输入代币合约地址 (CA) 添加到监控列表</label>
    <div class="input-row">
      <input type="text" id="caInput" placeholder="0x..." autocomplete="off" spellcheck="false">
      <button class="btn btn-primary" id="addBtn" onclick="addToken()">添加监控</button>
    </div>
    <div class="msg" id="statusMsg"></div>
  </div>

  <div class="section-title">监控中的代币 <span class="count" id="tokenCount">0</span></div>
  <div class="token-list" id="tokenList">
    <div class="empty-state">暂无监控代币，请在上方输入 CA 添加</div>
  </div>

  <div class="section-title" style="margin-top:24px">监控日志</div>
  <div class="log-section" id="logSection"></div>
</div>

<script>
const API = '';

function showMsg(text, type) {
  const el = document.getElementById('statusMsg');
  el.textContent = text;
  el.className = 'msg ' + type;
  if (type === 'success') setTimeout(() => { el.textContent = ''; }, 4000);
}

async function addToken() {
  const input = document.getElementById('caInput');
  const btn = document.getElementById('addBtn');
  const ca = input.value.trim();
  if (!ca) { showMsg('请输入合约地址', 'error'); return; }
  if (!/^0x[a-fA-F0-9]{40}$/.test(ca)) { showMsg('无效的合约地址格式', 'error'); return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="loading-spinner"></span>查询中...';
  showMsg('', '');

  try {
    const res = await fetch(API + '/api/tokens', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({address: ca}),
    });
    const data = await res.json();
    if (data.success) {
      showMsg(data.message, 'success');
      input.value = '';
      loadTokens();
    } else {
      showMsg(data.message || '添加失败', 'error');
    }
  } catch(e) {
    showMsg('请求失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '添加监控';
  }
}

async function removeToken(addr) {
  if (!confirm('确定移除该代币的监控？')) return;
  try {
    const res = await fetch(API + '/api/tokens/' + addr, {method: 'DELETE'});
    const data = await res.json();
    if (data.success) loadTokens();
  } catch(e) {
    console.error(e);
  }
}

function stateClass(state) {
  if (state === 0) return 'state-accumulating';
  if (state === 1) return 'state-streaming';
  if (state === 2) return 'state-snowball';
  return '';
}

async function loadTokens() {
  try {
    const res = await fetch(API + '/api/tokens');
    const data = await res.json();
    const list = document.getElementById('tokenList');
    const count = document.getElementById('tokenCount');
    count.textContent = data.tokens.length;

    if (data.tokens.length === 0) {
      list.innerHTML = '<div class="empty-state">暂无监控代币，请在上方输入 CA 添加</div>';
      return;
    }

    list.innerHTML = data.tokens.map(t => `
      <div class="token-card">
        <div class="token-info">
          <div>
            <span class="token-symbol">$${t.token_symbol || 'UNKNOWN'}</span>
            <span class="state-badge ${stateClass(t.state)}">${t.state_name}</span>
          </div>
          <div class="token-addr">${t.token_address}</div>
          <div class="token-meta">
            <span>💰 ${t.bnb_balance} BNB</span>
            <span>🐦 @${t.x_handle || '-'}</span>
            <span>🏦 ${t.vault_address ? t.vault_address.slice(0,8) + '...' : '-'}</span>
          </div>
          ${t.receiver ? `<div class="token-meta" style="margin-top:4px"><span>👤 认领者: <a href="https://bscscan.com/address/${t.receiver}" target="_blank" style="color:#60a5fa;text-decoration:none">${t.receiver.slice(0,6)}...${t.receiver.slice(-4)}</a></span></div>` : ''}
          <div class="links">
            <a href="https://flap.sh/bnb/${t.token_address}/taxinfo" target="_blank">Flap详情</a>
            <a href="https://bscscan.com/address/${t.vault_address}" target="_blank">BSCScan</a>
          </div>
        </div>
        <div class="token-actions">
          <button class="btn btn-danger" onclick="removeToken('${t.token_address}')">移除</button>
        </div>
      </div>
    `).join('');
  } catch(e) {
    console.error(e);
  }
}

async function loadLogs() {
  try {
    const res = await fetch(API + '/api/logs');
    const data = await res.json();
    const section = document.getElementById('logSection');
    const wasAtBottom = section.scrollTop + section.clientHeight >= section.scrollHeight - 30;

    section.innerHTML = data.logs.map(l => {
      let cls = 'log-info';
      if (l.level === 'WARNING') cls = 'log-warn';
      else if (l.level === 'ERROR') cls = 'log-error';
      else if (l.level === 'SUCCESS') cls = 'log-success';
      return `<div class="log-line"><span class="log-time">${l.time}</span> <span class="${cls}">[${l.level}]</span> ${l.message}</div>`;
    }).join('');

    if (wasAtBottom) section.scrollTop = section.scrollHeight;
  } catch(e) {}
}

document.getElementById('caInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') addToken();
});

loadTokens();
loadLogs();
setInterval(loadTokens, 10000);
setInterval(loadLogs, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/tokens", methods=["GET"])
def api_get_tokens():
    tokens = []
    with watch_tokens_lock:
        for addr in watch_tokens:
            cached = monitor.vault_cache.get(addr) if monitor else None
            if cached:
                tokens.append({
                    "token_address": cached.token_address,
                    "vault_address": cached.vault_address,
                    "state": cached.state,
                    "state_name": cached.state_name,
                    "description": cached.description,
                    "x_handle": cached.x_handle,
                    "bnb_balance": f"{cached.bnb_balance:.4f}",
                    "token_symbol": cached.token_symbol,
                    "receiver": cached.receiver or "",
                })
            else:
                tokens.append({
                    "token_address": addr,
                    "vault_address": "",
                    "state": -1,
                    "state_name": "查询中...",
                    "description": "",
                    "x_handle": "",
                    "bnb_balance": "0",
                    "token_symbol": "...",
                    "receiver": "",
                })
    return jsonify({"tokens": tokens})


@app.route("/api/tokens", methods=["POST"])
def api_add_token():
    data = request.get_json()
    addr = (data.get("address") or "").strip().lower()

    if not addr or not addr.startswith("0x") or len(addr) != 42:
        return jsonify({"success": False, "message": "无效的合约地址格式"})

    with watch_tokens_lock:
        if addr in watch_tokens:
            return jsonify({"success": False, "message": "该地址已在监控列表中"})

    if not monitor:
        return jsonify({"success": False, "message": "监控服务未初始化"})

    # 先查询 vault 信息
    vault_info = monitor.get_vault_info(addr)
    if vault_info is None:
        return jsonify({"success": False, "message": "该地址没有关联的 Gift Vault，或不是 FlapX Vault"})

    # 已认领的 CA 不纳入持续监控，仅告知状态
    if vault_info.is_claimed:
        receiver_display = vault_info.receiver or "未知"
        log_msg = f"${vault_info.token_symbol} ({addr}) 已被认领，认领者: {receiver_display}，无需监控"
        add_log("INFO", log_msg)
        logger.info(log_msg)

        if alert and event_loop:
            asyncio.run_coroutine_threadsafe(
                alert.send_already_claimed(
                    token_address=addr,
                    token_symbol=vault_info.token_symbol,
                    bnb_balance=f"{vault_info.bnb_balance:.4f}",
                    receiver=vault_info.receiver,
                ),
                event_loop,
            )

        return jsonify({
            "success": True,
            "message": f"${vault_info.token_symbol} 已被认领，认领者: {receiver_display}，无需监控",
        })

    with watch_tokens_lock:
        if addr not in watch_tokens:
            watch_tokens.append(addr)
    monitor.vault_cache[addr] = vault_info

    log_msg = f"CA: {addr}，${vault_info.token_symbol}，{vault_info.state_name}，金库状态监控中"
    add_log("SUCCESS", log_msg)
    logger.info(log_msg)

    # 发送 Telegram 通知
    if alert and event_loop:
        asyncio.run_coroutine_threadsafe(
            alert.send_monitoring_started(
                token_address=addr,
                token_symbol=vault_info.token_symbol,
                state_name=vault_info.state_name,
                bnb_balance=f"{vault_info.bnb_balance:.4f}",
            ),
            event_loop,
        )

    return jsonify({
        "success": True,
        "message": f"CA: {addr}，${vault_info.token_symbol}，金库状态监控中",
    })


@app.route("/api/tokens/<address>", methods=["DELETE"])
def api_remove_token(address):
    addr = address.strip().lower()
    token_symbol = ""
    with watch_tokens_lock:
        if addr in watch_tokens:
            watch_tokens.remove(addr)
            if monitor and addr in monitor.vault_cache:
                token_symbol = monitor.vault_cache[addr].token_symbol
                del monitor.vault_cache[addr]
            add_log("INFO", f"已移除监控: {addr}")
            logger.info(f"已移除监控: {addr}")

            # 发送 Telegram 通知
            if alert and event_loop:
                asyncio.run_coroutine_threadsafe(
                    alert.send_monitoring_removed(
                        token_address=addr,
                        token_symbol=token_symbol or "UNKNOWN",
                    ),
                    event_loop,
                )

            return jsonify({"success": True})
    return jsonify({"success": False, "message": "地址不在监控列表中"})


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    return jsonify({"logs": monitor_logs[-100:]})


# ============================================================
# 主程序入口
# ============================================================


def signal_handler(sig, frame):
    global running
    logger.info("收到退出信号，正在停止...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


async def monitor_loop():
    global monitor, alert, event_loop, watch_tokens

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    phone = os.getenv("TG_PHONE_NUMBER", "")
    target_group = int(os.getenv("TG_TARGET_GROUP", "0"))
    rpc_url = os.getenv("BSC_RPC_URL", BSC_RPC_ANKR)
    poll_interval = int(os.getenv("POLL_INTERVAL", "30"))
    watch_tokens_str = os.getenv("WATCH_TOKENS", "")

    if not api_id or not api_hash or not phone:
        logger.error("请设置 TG_API_ID、TG_API_HASH、TG_PHONE_NUMBER 环境变量")
        sys.exit(1)

    if not target_group:
        logger.error("请设置 TG_TARGET_GROUP 环境变量")
        sys.exit(1)

    # 初始化监控代币列表
    initial_tokens = [
        t.strip().lower()
        for t in watch_tokens_str.split(",")
        if t.strip()
    ]
    with watch_tokens_lock:
        for t in initial_tokens:
            if t not in watch_tokens:
                watch_tokens.append(t)

    # 初始化 Telegram
    alert = TelegramAlert(api_id, api_hash, phone, target_group)
    await alert.start()

    event_loop = asyncio.get_event_loop()

    # 初始化链上监控
    monitor = VaultMonitor(rpc_url)
    logger.info(f"BSC RPC 已连接，当前区块: {monitor.w3.eth.block_number}")
    logger.info(f"轮询间隔: {poll_interval} 秒")
    add_log("INFO", f"BSC RPC 已连接，轮询间隔: {poll_interval} 秒")

    # 初始化已有代币
    startup_details = []
    with watch_tokens_lock:
        tokens_snapshot = list(watch_tokens)
    for token_addr in tokens_snapshot:
        status, change_type = monitor.check_vault_changes(token_addr)
        if status:
            msg = (
                f"初始状态 - ${status.token_symbol} ({token_addr}): "
                f"{status.state_name}, 余额: {status.bnb_balance:.4f} BNB"
            )
            logger.info(msg)
            add_log("INFO", msg)
            startup_details.append({
                "symbol": status.token_symbol,
                "address": token_addr,
                "state": status.state_name,
                "balance": f"{status.bnb_balance:.4f}",
            })

    await alert.send_startup_message(len(tokens_snapshot), startup_details)
    add_log("SUCCESS", f"监控已启动，当前 {len(tokens_snapshot)} 个代币")

    last_scanned_block = monitor.w3.eth.block_number
    logger.info("开始监控循环...")

    try:
        while running:
            try:
                with watch_tokens_lock:
                    tokens_snapshot = list(watch_tokens)

                auto_discover = len(tokens_snapshot) == 0

                if auto_discover:
                    current_block = monitor.w3.eth.block_number
                    if current_block > last_scanned_block:
                        new_tokens = monitor.discover_new_vaults(
                            from_block=last_scanned_block + 1,
                            to_block=current_block,
                        )
                        for token_addr in new_tokens:
                            with watch_tokens_lock:
                                if token_addr not in watch_tokens:
                                    watch_tokens.append(token_addr)
                                    tokens_snapshot.append(token_addr)
                            logger.info(f"自动发现新代币: {token_addr}")
                            add_log("INFO", f"自动发现新代币: {token_addr}")
                        last_scanned_block = current_block

                for token_addr in tokens_snapshot:
                    status, change_type = monitor.check_vault_changes(token_addr)
                    if status is None:
                        continue

                    if change_type == "claimed":
                        await alert.send_vault_claimed_alert(
                            token_symbol=status.token_symbol,
                            token_address=status.token_address,
                            vault_address=status.vault_address,
                            x_handle=status.x_handle,
                            new_state=status.state_name,
                            bnb_balance=f"{status.bnb_balance:.4f}",
                            description=status.description,
                            receiver=status.receiver,
                        )
                        add_log("WARNING", f"🚨 金库被认领! ${status.token_symbol} ({token_addr})")
                    elif change_type == "snowball":
                        await alert.send_snowball_alert(
                            token_symbol=status.token_symbol,
                            token_address=status.token_address,
                            vault_address=status.vault_address,
                            x_handle=status.x_handle,
                            bnb_balance=f"{status.bnb_balance:.4f}",
                            description=status.description,
                        )
                        add_log("WARNING", f"❄️ 金库进入雪球模式! ${status.token_symbol} ({token_addr})")
                    elif change_type == "new" and auto_discover:
                        await alert.send_new_vault_alert(
                            token_symbol=status.token_symbol,
                            token_address=status.token_address,
                            vault_address=status.vault_address,
                            x_handle=status.x_handle,
                            bnb_balance=f"{status.bnb_balance:.4f}",
                            description=status.description,
                        )
                        add_log("INFO", f"📢 发现新金库: ${status.token_symbol} ({token_addr})")

                for _ in range(poll_interval):
                    if not running:
                        break
                    await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"监控循环异常: {e}", exc_info=True)
                add_log("ERROR", f"监控循环异常: {e}")
                await asyncio.sleep(10)
    finally:
        await alert.stop()
        logger.info("监控已停止")


def run_monitor():
    asyncio.run(monitor_loop())


if __name__ == "__main__":
    # 关闭 werkzeug 的请求日志刷屏
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # 在后台线程运行监控循环
    monitor_thread = threading.Thread(target=run_monitor, daemon=True)
    monitor_thread.start()

    # 等待 monitor 初始化完成
    import time as _time
    for _ in range(30):
        if monitor is not None:
            break
        _time.sleep(1)

    web_port = int(os.getenv("WEB_PORT", "5555"))
    logger.info(f"Web 界面启动: http://0.0.0.0:{web_port}")
    add_log("INFO", f"Web 界面已启动，端口: {web_port}")

    try:
        app.run(host="0.0.0.0", port=web_port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        logger.info("正在退出...")
        monitor_thread.join(timeout=5)
