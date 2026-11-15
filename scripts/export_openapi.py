import os
import sys
from pathlib import Path
from typing import Any

import yaml

# Garante valores mínimos de ambiente para importar os apps sem abortar
os.environ.setdefault("JWT_SECRET_KEY", "doc-generation-secret-key-min-32-chars")
os.environ.setdefault("SERVICE_NAME", "ecommerce-microservices")
os.environ.setdefault("ENVIRONMENT", "development")

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "services" / "payment_service"))

from services.catalog_service.main import app as catalog_app  # noqa: E402
from services.notification_service.main import app as notification_app  # noqa: E402
from services.order_service.main import app as order_app  # noqa: E402
from services.payment_service.main import app as payment_app  # noqa: E402


def export_specs() -> None:
    output_dir = ROOT_DIR / "docs" / "api"
    output_dir.mkdir(parents=True, exist_ok=True)

    services = [
        ("catalog-service", "http://localhost:8001", catalog_app),
        ("order-service", "http://localhost:8000", order_app),
        ("payment-service", "http://localhost:8002", payment_app),
        ("notification-service", "http://localhost:8003", notification_app),
    ]

    paths: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    unified_spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": "E-Commerce Distributed System API",
            "version": "1.0.0",
            "description": (
                "Especificação OpenAPI 3.0 consolidada dos microsserviços: "
                "Order, Catalog, Payment e Notification."
            ),
        },
        "servers": [
            {"url": "http://localhost:8000", "description": "Order Service"},
            {"url": "http://localhost:8001", "description": "Catalog Service"},
            {"url": "http://localhost:8002", "description": "Payment Service"},
            {"url": "http://localhost:8003", "description": "Notification Service"},
        ],
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": {}},
    }

    for name, server_url, app_instance in services:
        schema = app_instance.openapi()
        schema["openapi"] = "3.0.3"
        schema["servers"] = [{"url": server_url, "description": name}]

        # Salva especificação individual (ex: docs/api/catalog-service.yaml)
        service_file = output_dir / f"{name}.yaml"
        with open(service_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(schema, f, sort_keys=False, allow_unicode=True)

        # Mescla rotas e schemas no arquivo unificado docs/api/openapi.yaml
        for path, methods in schema.get("paths", {}).items():
            target_path = path if path not in paths else f"/{name}{path}"
            for method_data in methods.values():
                if isinstance(method_data, dict):
                    tags = method_data.setdefault("tags", [])
                    if isinstance(tags, list):
                        tags.insert(0, name)
            paths[target_path] = methods

        for comp_name, comp_val in (
            schema.get("components", {}).get("schemas", {}).items()
        ):
            schemas[comp_name] = comp_val

    unified_file = output_dir / "openapi.yaml"
    with open(unified_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(unified_spec, f, sort_keys=False, allow_unicode=True)

    print(f"Especificações geradas com sucesso em: {output_dir}")


if __name__ == "__main__":
    export_specs()
