import random 
import time
from datetime import datetime, timezone

def process_payment(order_data: dict) -> dict:
    # Simula o processamento bancário com latência e regras para PIX, cartão e boleto

    order_id = order_data.get("order_id")
    payment_method = order_data.get("payment_method", "credit_card")

    # 1. Simulador latência de rede/banco (500ms a 1500ms)
    latency = random.uniform(0.5, 1.5)
    time.sleep(latency)

    # 2. Taxa de sucesso controlada (ex: 85% de aprovação genérica)
    success = random.rando() < 0.85
    status = "payment.approved" if success else "payment.rejected"

    # 3. Montar o schema de saída exigido
    result_event = {
        "order_id": order_id,
        "payment_id": f"pay_{random.randint(100000, 999999)}",
        "status": status,
        "payment_method": payment_method,
        "processed_at": datetime.now(timezone.utc).isoformat()
    }

    return result_event