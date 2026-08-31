"""
RebalanceKeeper — KeeperHub MCP HTTP client.

Handles the MCP Streamable HTTP transport protocol:
  1. initialize  → get Mcp-Session-Id from response header
  2. notifications/initialized → handshake complete
  3. tools/call  → invoke KeeperHub MCP tools

Session IDs are valid for 24 hours and cached in-memory.
"""

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

import config  # 平铺结构: 同级模块导入 (绑定 config 模块名供下方 config.X 引用)


class MCPError(Exception):
    """Raised when the MCP server returns an error."""


class KeeperHubClient:
    """Thin HTTP client for the KeeperHub MCP server."""

    def __init__(
        self,
        url: str = config.KEEPERHUB_MCP_URL,
        api_key: str = config.KEEPERHUB_API_KEY,
    ):
        if not api_key:
            raise ValueError("KEEPERHUB_API_KEY is required. Set it in .env or config.")
        self.url = url
        self.api_key = api_key
        self._session_id: Optional[str] = None
        self._session_expires: float = 0
        self._msg_id = 0
        # Browser-like UA to pass Cloudflare fingerprint check
        self._ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )

    # ── Low-level transport ───────────────────────────────────

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _send(
        self,
        method: str,
        params: Dict[str, Any],
        msg_id: Optional[int] = None,
    ) -> Optional[Dict]:
        """Send one MCP JSON-RPC message. Auto-manages session."""
        # Ensure we have a valid session
        if method not in ("initialize", "notifications/initialized"):
            self._ensure_session()

        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if msg_id is not None:
            msg["id"] = msg_id

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": self._ua,
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        data = json.dumps(msg).encode()
        req = urllib.request.Request(self.url, data=data, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode().strip()
                # Capture session ID from initialize response
                sid = resp.headers.get("Mcp-Session-Id")
                if sid and not self._session_id:
                    self._session_id = sid
                    self._session_expires = time.time() + 23 * 3600  # 23h margin
                if not body:
                    return None  # notifications return empty body
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            # If session expired, reset and retry once
            if e.code in (400, 401) and "session" in body.lower():
                self._session_id = None
                if msg_id is not None:
                    return self._send(method, params, msg_id)
            raise MCPError(f"HTTP {e.code}: {body[:500]}") from e

    def _ensure_session(self):
        """Initialize MCP session if not already done or expired."""
        if self._session_id and time.time() < self._session_expires:
            return

        # Step 1: initialize
        result = self._send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "RebalanceKeeper", "version": "0.1.0"},
            },
            msg_id=self._next_id(),
        )
        if not result or "error" in result:
            raise MCPError(f"Initialize failed: {result}")

        # Step 2: notifications/initialized (no id = notification, no response)
        self._send("notifications/initialized", {})

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call an MCP tool by name. Returns the parsed result dict."""
        result = self._send(
            "tools/call",
            {"name": name, "arguments": arguments},
            msg_id=self._next_id(),
        )
        if not result:
            raise MCPError(f"Empty response for tool '{name}'")
        if "error" in result:
            err = result["error"]
            raise MCPError(f"Tool '{name}' error [{err.get('code')}]: {err.get('message')}")

        # MCP wraps results in content array
        content = result.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return {"raw": item["text"]}
        return {}

    # ── Aave V3 read actions (no wallet needed) ───────────────

    def get_user_account_data(self, user: str, network: str = None) -> Dict[str, str]:
        """Read Aave V3 account data: health factor, collateral, debt.

        Returns:
            {
                "totalCollateralBase": "0",
                "totalDebtBase": "0",
                "availableBorrowsBase": "0",
                "currentLiquidationThreshold": "0",
                "ltv": "0",
                "healthFactor": "115792089237316...",
            }
        """
        res = self._call_tool("execute_protocol_action", {
            "actionType": "aave-v3/get-user-account-data",
            "params": {
                "network": network or config.CHAIN_ID,
                "user": user,
            },
        })
        if res.get("success"):
            return res.get("result", {})
        raise MCPError(f"get-user-account-data failed: {res}")

    def get_user_reserve_data(self, user: str, asset: str, network: str = None) -> Dict:
        """Read per-asset Aave V3 position: supplied, debt, rates."""
        res = self._call_tool("execute_protocol_action", {
            "actionType": "aave-v3/get-user-reserve-data",
            "params": {
                "network": network or config.CHAIN_ID,
                "user": user,
                "asset": asset,
            },
        })
        if res.get("success"):
            return res.get("result", {})
        raise MCPError(f"get-user-reserve-data failed: {res}")

    # ── Aave V3 write actions (requires wallet) ───────────────

    def _decimals_for(self, asset: str) -> int:
        """Look up decimals for a token address."""
        for sym, info in config.TOKENS.items():
            if info.get("address", "").lower() == asset.lower():
                return info.get("decimals", 18)
        return 18

    def _to_base(self, amount: str, decimals: int = 18) -> str:
        """Convert human-readable amount to token's smallest unit."""
        try:
            val = float(amount)
            if val < 100:
                return str(int(val * (10 ** decimals)))
        except ValueError:
            pass
        return amount

    def supply(
        self,
        asset: str,
        amount: str,
        on_behalf_of: str = None,
        network: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Supply an asset as collateral to Aave V3."""
        params: Dict[str, Any] = {
            "network": network or config.CHAIN_ID,
            "asset": asset,
            "amount": self._to_base(amount, self._decimals_for(asset)),
            "onBehalfOf": on_behalf_of or config.WALLET_ADDRESS,
            "referralCode": "0",
        }
        return self._execute_with_retry("aave-v3/supply", params, idempotency_key)

    def borrow(
        self,
        asset: str,
        amount: str,
        on_behalf_of: str = None,
        interest_rate_mode: str = None,
        network: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Borrow an asset from Aave V3 against supplied collateral."""
        params: Dict[str, Any] = {
            "network": network or config.CHAIN_ID,
            "asset": asset,
            "amount": self._to_base(amount, self._decimals_for(asset)),
            "onBehalfOf": on_behalf_of or config.WALLET_ADDRESS,
            "referralCode": "0",
        }
        if interest_rate_mode:
            params["interestRateMode"] = interest_rate_mode
        return self._execute_with_retry("aave-v3/borrow", params, idempotency_key)

    def repay(
        self,
        asset: str,
        amount: str,
        on_behalf_of: str = None,
        interest_rate_mode: str = None,
        network: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Repay borrowed asset to Aave V3."""
        params: Dict[str, Any] = {
            "network": network or config.CHAIN_ID,
            "asset": asset,
            "amount": self._to_base(amount, self._decimals_for(asset)),
            "onBehalfOf": on_behalf_of or config.WALLET_ADDRESS,
            "referralCode": "0",
        }
        if interest_rate_mode:
            params["interestRateMode"] = interest_rate_mode
        return self._execute_with_retry("aave-v3/repay", params, idempotency_key)

    def set_collateral(
        self,
        asset: str,
        use_as_collateral: bool = True,
        network: str = None,
    ) -> Dict:
        """Enable/disable an asset as collateral in Aave V3."""
        params: Dict[str, Any] = {
            "network": network or config.CHAIN_ID,
            "asset": asset,
            "useAsCollateral": str(use_as_collateral).lower(),
        }
        return self._execute_with_retry("aave-v3/set-collateral", params, None)

    def withdraw(
        self,
        asset: str,
        amount: str,
        to: str = None,
        network: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Withdraw a supplied asset from Aave V3."""
        params: Dict[str, Any] = {
            "network": network or config.CHAIN_ID,
            "asset": asset,
            "amount": amount,
            "to": to or config.WALLET_ADDRESS,
        }
        return self._execute_with_retry("aave-v3/withdraw", params, idempotency_key)

    # ── Web3 utility actions ──────────────────────────────────

    def check_balance(self, address: str, network: str = None) -> Dict:
        """Get native token (ETH) balance of an address."""
        return self._call_tool("execute_protocol_action", {
            "actionType": "web3/check-balance",
            "params": {
                "network": network or config.CHAIN_ID,
                "address": address,
            },
        })

    def wrap_eth(
        self,
        amount_eth: str,
        network: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Wrap native ETH into WETH via WETH.deposit() (payable).

        Aave V3 requires ERC20 collateral; native ETH must be wrapped first.
        """
        params: Dict[str, Any] = {
            "contract_address": config.token_addr("WETH"),
            "chain_id": network or config.CHAIN_ID,
            "function_name": "deposit",
            "function_args": "[]",
            "value": amount_eth,
        }
        if idempotency_key:
            params["idempotency_key"] = idempotency_key
        return self._call_tool("execute_contract_call", params)

    def approve(
        self,
        token: str,
        spender: str,
        amount: str,
        network: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Approve spender to spend ERC20 token (e.g. WETH for Aave Pool)."""
        params: Dict[str, Any] = {
            "contract_address": token,
            "chain_id": network or config.CHAIN_ID,
            "function_name": "approve",
            "function_args": json.dumps([spender, amount]),
        }
        if idempotency_key:
            params["idempotency_key"] = idempotency_key
        return self._call_tool("execute_contract_call", params)

    def get_token_balance(
        self,
        token: str,
        owner: str,
        network: str = None,
    ) -> str:
        """Get ERC20 balance of owner (view call). Returns raw integer string."""
        params: Dict[str, Any] = {
            "contract_address": token,
            "chain_id": network or config.CHAIN_ID,
            "function_name": "balanceOf",
            "function_args": json.dumps([owner]),
        }
        res = self._call_tool("execute_contract_call", params)
        if isinstance(res, dict):
            return str(res.get("result", "0"))
        return str(res)

    # ── Conditional execution (the killer feature) ───────────

    def execute_check_and_execute(
        self,
        contract_address: str,
        function_name: str,
        condition_operator: str,
        condition_value: str,
        action_contract: str,
        action_function: str,
        function_args: str = "[]",
        action_function_args: str = "[]",
        chain_id: str = None,
        abi: str = None,
        action_abi: str = None,
        idempotency_key: str = None,
    ) -> Dict:
        """Read a contract value, evaluate condition, execute action if met.

        This is the core KeeperHub primitive for conditional on-chain execution.
        """
        params: Dict[str, Any] = {
            "contract_address": contract_address,
            "chain_id": chain_id or config.CHAIN_ID,
            "function_name": function_name,
            "function_args": function_args,
            "condition": {
                "operator": condition_operator,
                "value": condition_value,
            },
            "action": {
                "contract_address": action_contract,
                "function_name": action_function,
                "function_args": action_function_args,
            },
        }
        if abi:
            params["abi"] = abi
        if action_abi:
            params["action"]["abi"] = action_abi
        if idempotency_key:
            params["idempotency_key"] = idempotency_key

        return self._call_tool("execute_check_and_execute", params)

    # ── Internal helpers ──────────────────────────────────────

    def _execute_with_retry(
        self,
        action_type: str,
        params: Dict[str, Any],
        idempotency_key: str = None,
        max_retries: int = 3,
    ) -> Dict:
        """Execute a protocol action with automatic retry on transient failures."""
        import time as _time

        last_error = None
        for attempt in range(max_retries):
            try:
                call_params = dict(params)
                if idempotency_key:
                    call_params["idempotency_key"] = f"{idempotency_key}_{attempt}"
                res = self._call_tool("execute_protocol_action", {
                    "actionType": action_type,
                    "params": call_params,
                })
                if res.get("success") or res.get("transactionHash"):
                    return res
                last_error = res.get("error", str(res))
            except MCPError as e:
                last_error = str(e)

            if attempt < max_retries - 1:
                _time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s, 4s

        raise MCPError(f"{action_type} failed after {max_retries} retries: {last_error}")
