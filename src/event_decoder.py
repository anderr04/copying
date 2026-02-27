"""
event_decoder.py – Decodificador de eventos OrderFilled del CTFExchange.

Versión ajustada para la prueba de escucha (latencia + precisión).

ABI del evento
--------------
    event OrderFilled(
        bytes32 indexed orderHash,   // topic[1]
        address indexed maker,       // topic[2]
        address indexed taker,       // topic[3]
        uint256 makerAssetId,        // data[0:32]
        uint256 takerAssetId,        // data[32:64]
        uint256 makerAmountFilled,   // data[64:96]   (raw, 6 decimales)
        uint256 takerAmountFilled,   // data[96:128]  (raw, 6 decimales)
        uint256 fee                  // data[128:160] (raw)
    )

topic0 = keccak256 de la firma completa.
Campos indexados en topics[1..3], datos ABI-encoded en data (5×uint256=160B).

Proxy Wallets
-------------
En Polymarket los usuarios operan a través de proxy wallets (Gnosis Safe).
El maker/taker en el evento es la PROXY, no la dirección pública del usuario.
Para identificar a la ballena hay que comprobar también tx.from (el EOA que
firma la transacción de origen) o mantener un mapeo proxy → propietario.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Decimales iguales: USDC = 6, CTF tokens = 6 → precio = raw_usdc / raw_tokens
USDC_DECIMALS = 6
TOKEN_DECIMALS = 6

# ── Event Signature ──────────────────────────────────────────────────
_ORDER_FILLED_SIG = (
    "OrderFilled(bytes32,address,address,"
    "uint256,uint256,uint256,uint256,uint256)"
)

_topic0_cache: Optional[bytes] = None


def get_order_filled_topic0() -> bytes:
    """topic0 = keccak256(event signature).  Cacheado tras primera llamada."""
    global _topic0_cache
    if _topic0_cache is not None:
        return _topic0_cache
    from web3 import Web3
    _topic0_cache = Web3.keccak(text=_ORDER_FILLED_SIG)
    return _topic0_cache


def get_order_filled_topic0_hex() -> str:
    return "0x" + get_order_filled_topic0().hex()


# ── Structured output ────────────────────────────────────────────────

@dataclass
class OrderFilledEvent:
    """Evento OrderFilled crudo decodificado."""
    tx_hash: str
    block_number: int
    log_index: int
    contract_address: str       # dirección del contrato que emitió el log
    order_hash: str
    maker: str                  # dirección de la proxy/wallet maker
    taker: str                  # dirección de la proxy/wallet taker
    maker_asset_id: int         # uint256
    taker_asset_id: int         # uint256
    maker_amount_filled: int    # raw 6 dec
    taker_amount_filled: int    # raw 6 dec
    fee: int                    # raw
    timestamp: float = field(default_factory=time.time)


@dataclass
class WhaleTradeSignal:
    """
    Señal enriquecida lista para el engine / logging determinista.
    """
    tx_hash: str
    block_number: int
    whale_address: str          # dirección que matcheó (EOA o proxy)
    whale_label: str            # "DrPufferfish", etc.
    whale_role: str             # "maker", "taker" o "tx_sender"

    # Trade
    token_id: str               # conditional token ID (decimal string)
    action: str                 # "BUY" o "SELL"
    price: float                # precio de ejecución P_b (0-1)
    size_tokens: float          # tokens (human-readable)
    size_usd: float             # USDC (human-readable)
    fee_usd: float

    # Event context
    maker: str = ""             # proxy maker del evento
    taker: str = ""             # proxy taker del evento
    tx_from: str = ""           # EOA que firmó la tx

    # Market info (enriquecido por token_registry)
    condition_id: str = ""
    market_question: str = ""
    outcome: str = ""           # "Yes" / "No"
    timestamp: float = field(default_factory=time.time)


# ── Decoder ──────────────────────────────────────────────────────────

class EventDecoder:
    """
    Decodifica logs crudos de Polygon en OrderFilledEvent y WhaleTradeSignal.
    """

    def __init__(self):
        self._topic0_hex: Optional[str] = None

    @property
    def topic0_hex(self) -> str:
        if self._topic0_hex is None:
            self._topic0_hex = get_order_filled_topic0_hex()
        return self._topic0_hex

    # ── Decode crudo ─────────────────────────────────────────────

    def decode_log(self, log: dict) -> Optional[OrderFilledEvent]:
        """
        Decodifica un log dict (eth_subscribe result o eth_getLogs entry).
        Retorna None si no es un OrderFilled válido.
        """
        try:
            topics = log.get("topics", [])
            if len(topics) < 4:
                return None

            # Verificar topic0
            topic0 = topics[0]
            if isinstance(topic0, bytes):
                topic0 = "0x" + topic0.hex()
            if topic0.lower() != self.topic0_hex.lower():
                return None

            # Campos indexados
            order_hash = self._to_hex(topics[1])
            maker = self._topic_to_address(topics[2])
            taker = self._topic_to_address(topics[3])

            # Data (5 × uint256 = 160 bytes)
            data_hex = log.get("data", "0x")
            if isinstance(data_hex, bytes):
                data_hex = "0x" + data_hex.hex()
            data_bytes = bytes.fromhex(data_hex[2:])

            if len(data_bytes) < 160:
                logger.warning(
                    "OrderFilled data cortísimo: %d bytes (necesita 160)",
                    len(data_bytes),
                )
                return None

            from eth_abi import decode as abi_decode
            (
                maker_asset_id,
                taker_asset_id,
                maker_amount_filled,
                taker_amount_filled,
                fee_raw,
            ) = abi_decode(
                ["uint256", "uint256", "uint256", "uint256", "uint256"],
                data_bytes[:160],
            )

            # Block / logIndex / txHash
            block_num_raw = log.get("blockNumber", "0x0")
            block_number = (
                int(block_num_raw, 16)
                if isinstance(block_num_raw, str)
                else int(block_num_raw)
            )
            log_idx_raw = log.get("logIndex", "0x0")
            log_index = (
                int(log_idx_raw, 16)
                if isinstance(log_idx_raw, str)
                else int(log_idx_raw)
            )
            tx_hash = self._to_hex(log.get("transactionHash", "0x"))

            # Dirección del contrato
            contract_addr = log.get("address", "")
            if isinstance(contract_addr, bytes):
                contract_addr = "0x" + contract_addr.hex()

            return OrderFilledEvent(
                tx_hash=tx_hash,
                block_number=block_number,
                log_index=log_index,
                contract_address=contract_addr,
                order_hash=order_hash,
                maker=maker,
                taker=taker,
                maker_asset_id=maker_asset_id,
                taker_asset_id=taker_asset_id,
                maker_amount_filled=maker_amount_filled,
                taker_amount_filled=taker_amount_filled,
                fee=fee_raw,
            )

        except Exception as exc:
            logger.error("Error decodificando OrderFilled: %s", exc, exc_info=True)
            return None

    # ── Construir señal whale ────────────────────────────────────

    def to_whale_signal(
        self,
        event: OrderFilledEvent,
        whale_address: str,
        whale_label: str,
        whale_role: str = "maker",
        tx_from: str = "",
    ) -> Optional[WhaleTradeSignal]:
        """
        Convierte OrderFilledEvent → WhaleTradeSignal.

        Lógica de dirección BUY/SELL:
            assetId = 0 → USDC (colateral)
            assetId ≠ 0 → conditional token
            Si el whale ENTREGA el token → SELL
            Si el whale RECIBE el token  → BUY

        whale_role indica cómo se identificó ("maker", "taker", "tx_sender").
        Si whale_role = "tx_sender", el whale firmó la tx pero la proxy es la
        que aparece como maker/taker.  Heurística: asumimos que el whale
        controla al taker (que "toma" la orden resting del maker).
        """
        whale_lower = whale_address.lower()

        # ¿El whale es el maker o el taker en el evento?
        if event.maker.lower() == whale_lower:
            whale_is_maker = True
        elif event.taker.lower() == whale_lower:
            whale_is_maker = False
        else:
            # whale_role = "tx_sender": la dirección no está en maker/taker
            # (opera vía proxy).  Heurística conservadora: tx.from = taker.
            whale_is_maker = False

        # Identificar qué assetId es el conditional token
        maker_is_token = event.maker_asset_id != 0
        taker_is_token = event.taker_asset_id != 0

        if maker_is_token and not taker_is_token:
            token_id = event.maker_asset_id
            token_amount = event.maker_amount_filled
            usdc_amount = event.taker_amount_filled
        elif taker_is_token and not maker_is_token:
            token_id = event.taker_asset_id
            token_amount = event.taker_amount_filled
            usdc_amount = event.maker_amount_filled
        elif maker_is_token and taker_is_token:
            # Trade YES↔NO (poco común)
            if whale_is_maker:
                token_id = event.maker_asset_id
                token_amount = event.maker_amount_filled
                usdc_amount = event.taker_amount_filled
            else:
                token_id = event.taker_asset_id
                token_amount = event.taker_amount_filled
                usdc_amount = event.maker_amount_filled
        else:
            logger.warning(
                "Ambos assetIds=0 → evento inválido. tx=%s", event.tx_hash
            )
            return None

        if token_amount <= 0:
            logger.warning(
                "token_amount=0, imposible calcular precio. tx=%s", event.tx_hash
            )
            return None

        price = usdc_amount / token_amount

        # Dirección: whale ENTREGA token → SELL, RECIBE → BUY
        if whale_is_maker:
            whale_provides_token = (event.maker_asset_id == token_id)
        else:
            whale_provides_token = (event.taker_asset_id == token_id)

        action = "SELL" if whale_provides_token else "BUY"

        # Human-readable
        size_tokens = token_amount / (10 ** TOKEN_DECIMALS)
        size_usd = usdc_amount / (10 ** USDC_DECIMALS)
        fee_usd = event.fee / (10 ** USDC_DECIMALS)

        return WhaleTradeSignal(
            tx_hash=event.tx_hash,
            block_number=event.block_number,
            whale_address=whale_address,
            whale_label=whale_label,
            whale_role=whale_role,
            token_id=str(token_id),
            action=action,
            price=price,
            size_tokens=size_tokens,
            size_usd=size_usd,
            fee_usd=fee_usd,
            maker=event.maker,
            taker=event.taker,
            tx_from=tx_from,
        )

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _to_hex(value) -> str:
        if isinstance(value, bytes):
            return "0x" + value.hex()
        return str(value)

    @staticmethod
    def _topic_to_address(topic) -> str:
        """Extrae dirección de un topic de 32 bytes (left-padded)."""
        from web3 import Web3
        if isinstance(topic, bytes):
            hex_str = topic.hex()
        else:
            hex_str = str(topic)
            if hex_str.startswith("0x"):
                hex_str = hex_str[2:]
        addr_hex = hex_str[-40:]
        return Web3.to_checksum_address("0x" + addr_hex)
