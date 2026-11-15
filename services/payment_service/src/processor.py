import os
import random
import time
from datetime import UTC, datetime


def process_payment(order_data: dict) -> dict:
    # Simula o processamento bancário com latência e regras para PIX, cartão e boleto

    order_id = order_data.get("order_id")
    payment_method = order_data.get("payment_method", "credit_card")

    # Latência simulada (desativada ou reduzida se TESTING/FAST_TESTS estiver ativo)
    simulated_delay = float(os.getenv("PAYMENT_SIMULATED_DELAY", "0.2"))
    if simulated_delay > 0:
        time.sleep(simulated_delay)

    # 2. Taxa de sucesso controlada (ex: 85% de aprovação genérica)
    success = random.random() < 0.85
    status = "payment.approved" if success else "payment.rejected"

    # 3. Montar o schema de saída exigido
    result_event = {
        "order_id": order_id,
        "payment_id": f"pay_{random.randint(100000, 999999)}",
        "status": status,
        "payment_method": payment_method,
        "processed_at": datetime.now(UTC).isoformat(),
    }

    return result_event
