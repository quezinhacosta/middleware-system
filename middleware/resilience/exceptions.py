class MiddlewareTimeoutError(Exception):
    """Lançada quando uma operação excede o limite de tempo."""

    def __init__(self, *, url: str | None = None, timeout: float,
                 elapsed: float, operation: str | None = None) -> None:
        self.url = url
        self.timeout = timeout
        self.elapsed = elapsed
        self.operation = operation
        target = url or operation or "operação desconhecida"
        super().__init__(
            f"Timeout de {timeout:.2f}s excedido após {elapsed:.2f}s em {target}"
        )
