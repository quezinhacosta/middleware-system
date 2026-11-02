import logging
import math
import os
from enum import StrEnum

logger = logging.getLogger(__name__)


class Criticality(str, StrEnum):
    CATALOG = "catalog"
    PAYMENT = "payment"
    DEFAULT = "default"


_DEFAULTS: dict[Criticality, float] = {
    Criticality.CATALOG: 1.5,
    Criticality.PAYMENT: 5.0,
    Criticality.DEFAULT: 3.0,
}

_ENV_VARS: dict[Criticality, str] = {
    Criticality.CATALOG: "TIMEOUT_CATALOG_SECONDS",
    Criticality.PAYMENT: "TIMEOUT_PAYMENT_SECONDS",
    Criticality.DEFAULT: "TIMEOUT_DEFAULT_SECONDS",
}


def get_timeout(criticality: Criticality = Criticality.DEFAULT) -> float:
    """Lê o limite da env var; valor ausente ou inválido cai no default."""
    default = _DEFAULTS[criticality]
    env_name = _ENV_VARS[criticality]
    raw = os.getenv(env_name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s inválida (%r); usando %.2fs", env_name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        logger.warning("%s deve ser > 0 (%r); usando %.2fs", env_name, raw, default)
        return default
    return value
