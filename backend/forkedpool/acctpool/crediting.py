"""Crediting of a pool payment to its Bitcart invoice.

The plugin does not change invoice rows itself. It gives the payment to Bitcart's own PaymentProcessor, the same
way PaymentProcessor.new_payment_handler does for an event of a stock daemon. Invoice states, IPN, webhooks and
notifications stay stock. Bitcart completes the invoice at the store's transaction speed; it reads the
confirmations through the get_request filter (hooks.py).

A result with credited=False means that Bitcart did not take the payment: the caller records it as a late
payment. A reason in CLOSED_REASONS also closes the address for new money (a deleted invoice counts as closed).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dishka import AsyncContainer, Scope
from sqlalchemy import select

from api import models as bitcart_models
from api.db import AsyncSession
from api.invoices import InvoiceStatus, convert_status
from api.logging import get_exception_message, get_logger
from api.services.crud.invoices import InvoiceService
from api.services.crud.repositories.invoices import InvoiceRepository
from api.services.payment_processor import PaymentProcessor
from api.services.plugin_registry import PluginRegistry

from .lifecycle import CLOSING

logger = get_logger(__name__)

# Bitcart takes a payment for these invoice statuses only
OPEN_STATUSES = (InvoiceStatus.PENDING, InvoiceStatus.PAID, InvoiceStatus.CONFIRMED)
PAYMENT_STATUSES = (InvoiceStatus.PENDING, InvoiceStatus.UNCONFIRMED, InvoiceStatus.COMPLETE)
CLOSED_REASONS = {f"invoice_{status}" for status in CLOSING} | {"invoice_missing"}


@dataclass(frozen=True)
class CreditResult:
    credited: bool  # Bitcart took the data: the invoice was open
    reason: str | None = None  # why not: not_found, bad_status, error, invoice_<status>
    status_after: str | None = None


async def credit(container: AsyncContainer, invoice_id: str, lookup: str, request: Any) -> CreditResult:
    """request(method) -> the answer of the get_request filter for that payment method: status 0 (part payment),
    7 (paid, not confirmed) or 3 (paid, confirmed), tx_hashes, sent_amount (coin units), as a stock daemon."""
    try:
        return await _credit(container, invoice_id, lookup, request)
    except Exception as e:
        logger.error(f"acctpool: credit failed for {lookup}: {get_exception_message(e)}")
        return CreditResult(False, "error")


async def _credit(container: AsyncContainer, invoice_id: str, lookup: str, request: Any) -> CreditResult:
    methods, invoices, wallets = bitcart_models.PaymentMethod, bitcart_models.Invoice, bitcart_models.Wallet
    async with container(scope=Scope.REQUEST) as request_container:
        session = await request_container.get(AsyncSession)
        invoice_service = await request_container.get(InvoiceService)
        payment_processor = await request_container.get(PaymentProcessor)
        plugin_registry = await request_container.get(PluginRegistry)
        # PaymentProcessor.get_pending_invoices_query without the status filter: a closed invoice must be found
        # too, so that the caller learns why the payment was refused
        query = (
            select(methods, invoices, wallets)
            .where(methods.invoice_id == invoices.id)
            .where(methods.wallet_id == wallets.id)
            .where(methods.invoice_id == invoice_id)
            .where(methods.lookup_field == lookup)
            .options(*InvoiceRepository.LOAD_OPTIONS)
            .limit(1)
            .with_for_update()
        )
        data = (await session.execute(query)).first()
        if not data:
            missing = await session.get(invoices, invoice_id) is None
            return CreditResult(False, "invoice_missing" if missing else "not_found")
        method, invoice, wallet = data
        answer = await request(method)
        status, tx_hashes, sent_amount = answer["status"], answer["tx_hashes"], Decimal(answer["sent_amount"])
        payment_status = convert_status(status)
        if payment_status not in PAYMENT_STATUSES:
            return CreditResult(False, "bad_status")
        await invoice_service.load_one(invoice)
        before = invoice.status
        if before not in OPEN_STATUSES:
            return CreditResult(False, f"invoice_{before}", before)
        if is_repeat(invoice, method, payment_status, tx_hashes, sent_amount):
            return CreditResult(True, None, before)
        if before == InvoiceStatus.CONFIRMED:
            # Paid already: money that Bitcart's sent amount does not have (or on another method) is late.
            if method.id != invoice.payment_id or sent_amount > Decimal(invoice.sent_amount or 0):
                return CreditResult(False, f"invoice_{before}", before)
            # process_electrum_status refuses this status. The stock path for it is new_block_handler: read the
            # confirmations again and let Bitcart decide. Same conditions and same arguments as there.
            if payment_status == InvoiceStatus.COMPLETE and not method.lightning:
                confirmations = await payment_processor.get_confirmations(method, wallet)
                if confirmations != method.confirmations:
                    await payment_processor.update_invoice_confirmations(
                        invoice,
                        method,
                        wallet,
                        confirmations,
                        invoice.tx_hashes,
                        Decimal(invoice.sent_amount or 0),
                        di_context=request_container,
                    )
        else:
            await plugin_registry.run_hook(
                "new_payment", invoice, method, wallet, status, payment_status, tx_hashes, sent_amount
            )
            await payment_processor.process_electrum_status(
                invoice, method, wallet, status, tx_hashes, sent_amount, di_context=request_container
            )
        return CreditResult(True, None, invoice.status)


def is_repeat(invoice: Any, method: Any, payment_status: str, tx_hashes: list[str], sent_amount: Decimal) -> bool:
    """A part payment (status 0) is the one stock path that writes and notifies again on every call."""
    if payment_status != InvoiceStatus.PENDING:
        return False
    if sent_amount <= 0 or invoice.status != InvoiceStatus.PENDING:
        return True  # Bitcart does nothing with it
    return (
        getattr(invoice, "payment_id", None) == method.id
        and invoice.sent_amount == sent_amount
        and list(invoice.tx_hashes or []) == tx_hashes
    )
