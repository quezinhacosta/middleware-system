import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from middleware.auth.service_auth import (
    ALGORITHM,
    SERVICE_KEY_HEADER,
    ServiceAuthConfigError,
    ServiceAuthenticator,
    ServiceAuthError,
    ServiceForbiddenError,
    ServiceIdentity,
    ServiceTokenProvider,
    get_authenticator,
    require_internal_service,
)


@pytest.fixture
def auth(secret):
    return ServiceAuthenticator(secret)


# ---------- configuração ----------


@pytest.mark.parametrize("segredo", ["", "curto", "a" * 31])
def test_segredo_fraco_ou_ausente_falha_rapido(segredo):
    with pytest.raises(ServiceAuthConfigError):
        ServiceAuthenticator(segredo)


def test_segredo_com_32_bytes_e_aceito():
    ServiceAuthenticator("a" * 32)


def test_get_authenticator_sem_env_falha(monkeypatch):
    monkeypatch.delenv("INTERNAL_SERVICE_SECRET", raising=False)
    get_authenticator.cache_clear()
    with pytest.raises(ServiceAuthConfigError):
        get_authenticator()
    get_authenticator.cache_clear()


def test_get_authenticator_le_ttl_do_ambiente(s2s_env, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_TTL", "120")
    get_authenticator.cache_clear()
    assert get_authenticator().ttl_seconds == 120


def test_get_authenticator_e_singleton(s2s_env):
    assert get_authenticator() is get_authenticator()


# ---------- geração e validação ----------


def test_token_valido_retorna_identidade(auth):
    token, exp = auth.generate_token("order-service", "catalog-service")
    identidade = auth.validate_token(token, "catalog-service")
    assert isinstance(identidade, ServiceIdentity)
    assert identidade.service == "order-service"
    assert identidade.token_id
    assert exp > time.time()


def test_token_contem_claims_obrigatorios(auth, secret):
    token, _ = auth.generate_token("order-service", "catalog-service")
    claims = jwt.decode(
        token, secret, algorithms=[ALGORITHM], audience="catalog-service"
    )
    assert {"iss", "aud", "iat", "exp", "jti"} <= claims.keys()


def test_cada_token_tem_jti_unico(auth):
    jtis = set()
    for _ in range(20):
        token, _ = auth.generate_token("a", "b")
        jtis.add(auth.validate_token(token, "b").token_id)
    assert len(jtis) == 20


def test_audiencia_errada_e_rejeitada(auth):
    token, _ = auth.generate_token("order-service", "catalog-service")
    with pytest.raises(ServiceAuthError):
        auth.validate_token(token, "payment-service")


def test_token_expirado_e_rejeitado(secret):
    # ttl negativo: já nasce expirado, além do leeway de 5s
    expirador = ServiceAuthenticator(secret, ttl_seconds=-60)
    token, _ = expirador.generate_token("order-service", "catalog-service")
    with pytest.raises(ServiceAuthError):
        expirador.validate_token(token, "catalog-service")


def test_assinatura_com_outro_segredo_e_rejeitada(auth):
    outro = ServiceAuthenticator("outro-segredo-de-teste-com-32-bytes-ok!!")
    token, _ = outro.generate_token("order-service", "catalog-service")
    with pytest.raises(ServiceAuthError):
        auth.validate_token(token, "catalog-service")


def test_token_malformado_e_rejeitado(auth):
    with pytest.raises(ServiceAuthError):
        auth.validate_token("isso.nao.e-jwt", "catalog-service")


def test_algoritmo_none_e_rejeitado(auth):
    claims = {
        "iss": "order-service",
        "aud": "catalog-service",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "jti": "x",
    }
    token = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(ServiceAuthError):
        auth.validate_token(token, "catalog-service")


def test_claim_obrigatoria_ausente_e_rejeitada(auth, secret):
    claims = {
        "iss": "order-service",
        "aud": "catalog-service",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }  # sem jti
    token = jwt.encode(claims, secret, algorithm=ALGORITHM)
    with pytest.raises(ServiceAuthError):
        auth.validate_token(token, "catalog-service")


def test_emissor_fora_da_allowlist_e_proibido(auth):
    token, _ = auth.generate_token("payment-service", "catalog-service")
    with pytest.raises(ServiceForbiddenError):
        auth.validate_token(token, "catalog-service", allowed_issuers={"order-service"})


def test_emissor_na_allowlist_passa(auth):
    token, _ = auth.generate_token("order-service", "catalog-service")
    identidade = auth.validate_token(
        token, "catalog-service", allowed_issuers=["order-service"]
    )
    assert identidade.service == "order-service"


def test_forbidden_e_subclasse_de_auth_error():
    assert issubclass(ServiceForbiddenError, ServiceAuthError)


# ---------- ServiceTokenProvider ----------


def test_provider_reutiliza_token_em_cache(auth):
    provider = ServiceTokenProvider("order-service", auth)
    assert provider.get_token("catalog-service") == provider.get_token(
        "catalog-service"
    )


def test_provider_gera_token_separado_por_audiencia(auth):
    provider = ServiceTokenProvider("order-service", auth)
    assert provider.get_token("catalog-service") != provider.get_token(
        "payment-service"
    )


def test_provider_renova_token_proximo_da_expiracao(secret):
    # ttl (5s) menor que REFRESH_MARGIN_SECONDS (10s): sempre renova
    provider = ServiceTokenProvider(
        "order-service", ServiceAuthenticator(secret, ttl_seconds=5)
    )
    assert provider.get_token("catalog-service") != provider.get_token(
        "catalog-service"
    )


def test_provider_auth_headers_usa_header_padrao(auth):
    provider = ServiceTokenProvider("order-service", auth)
    headers = provider.auth_headers("catalog-service")
    assert list(headers) == [SERVICE_KEY_HEADER]
    assert (
        auth.validate_token(headers[SERVICE_KEY_HEADER], "catalog-service").service
        == "order-service"
    )


# ---------- dependência FastAPI ----------


@pytest.fixture
def client(s2s_env):
    app = FastAPI()

    @app.get("/interno")
    def interno(
        ident: ServiceIdentity = Depends(
            require_internal_service("catalog-service", {"order-service"})
        ),
    ):
        return {"caller": ident.service}

    return TestClient(app)


def _token(secret, issuer="order-service", audience="catalog-service"):
    token, _ = ServiceAuthenticator(secret).generate_token(issuer, audience)
    return token


def test_rota_sem_credencial_retorna_401(client):
    assert client.get("/interno").status_code == 401


def test_rota_com_token_valido_retorna_200(client, secret):
    r = client.get("/interno", headers={SERVICE_KEY_HEADER: _token(secret)})
    assert r.status_code == 200
    assert r.json() == {"caller": "order-service"}


def test_rota_com_token_invalido_retorna_401(client):
    r = client.get("/interno", headers={SERVICE_KEY_HEADER: "lixo"})
    assert r.status_code == 401


def test_rota_com_audiencia_errada_retorna_401(client, secret):
    r = client.get(
        "/interno",
        headers={SERVICE_KEY_HEADER: _token(secret, audience="payment-service")},
    )
    assert r.status_code == 401


def test_rota_com_chamador_nao_autorizado_retorna_403(client, secret):
    r = client.get(
        "/interno",
        headers={SERVICE_KEY_HEADER: _token(secret, issuer="payment-service")},
    )
    assert r.status_code == 403


def test_resposta_de_erro_nao_vaza_detalhes(client):
    r = client.get("/interno", headers={SERVICE_KEY_HEADER: "lixo"})
    assert "lixo" not in r.text
    assert "PyJWT" not in r.text and "DecodeError" not in r.text
