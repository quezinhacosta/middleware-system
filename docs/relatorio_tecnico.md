# Middleware Distribuído para E-commerce: Relatório Técnico

**Disciplina:** Arquitetura de Sistemas Distribuídos, 2º GQ de 2026
**Instituição:** Universidade Católica de Pernambuco (UNICAP)
**Professor:** Pedro Ximenes
**Equipe:** Quezia Costa, Lettycia Vitória, Lucas Mendes Nóbrega e João Victor
**Repositório:** github.com/quezinhacosta/middleware-system

---

## 1. Introdução e contexto

Aplicações de comércio eletrônico são compostas por serviços que se comunicam pela rede: catálogo, pedidos, pagamento e notificações. Nesse cenário, falhas parciais, latência e entregas duplicadas são a regra, e não exceção. O middleware é a camada que trata esses problemas sem que a lógica de negócio de cada serviço precise conhecê-los.

Este projeto implementa essa camada do zero, em Python, para um domínio de e-commerce. A lógica de negócio dos serviços é deliberadamente simples, com dados em memória e processamento de pagamento simulado. O foco está no middleware: comunicação síncrona e assíncrona, resiliência, segurança, observabilidade e tolerância a falhas. Bibliotecas de apoio, como FastAPI, pika e prometheus-client, foram usadas. Nenhum middleware pronto foi adotado como solução principal, como Kong, Istio ou API Gateway.

O resultado é uma stack de quatro serviços de negócio, um broker RabbitMQ e um conjunto de componentes compartilhados em `middleware/`, tudo orquestrado por Docker Compose. Os testes automatizados somam 398 unitários e 15 de integração, e a CI roda lint, tipagem e testes a cada push.

---

## 2. Arquitetura da solução

### 2.1 Serviços e componentes

| Componente | Responsabilidade | Porta |
|---|---|---|
| Order Service | Recebe pedidos, valida produtos no catálogo (REST) e encaminha o pagamento | 8000 (host) |
| Catalog Service | Produtos, preços e reserva/liberação de estoque para o padrão SAGA | 8001 (rede interna) |
| Payment Service | Autoriza pagamentos por HTTP e processa a fila `order.created` | 8002 (rede interna) |
| Notification Service | Consome eventos de pagamento e cancelamento e registra as notificações | 8002 (rede interna) |
| Message Broker (RabbitMQ) | Mensageria assíncrona, com exchanges, filas duráveis e DLQ | 15672 (painel) |
| Prometheus | Coleta de métricas a cada 5 s, com retenção de 15 dias | 9090 |
| Grafana | Dashboard provisionado a partir de arquivos | 3000 |

O pacote `middleware/` reúne os componentes compartilhados: autenticação e RBAC, configuração, cliente HTTP distribuído, circuit breaker, retry, timeout, fallback, produtor e consumidor AMQP, idempotência, correlation ID, logs JSON, métricas e healthchecks.

### 2.2 Fluxos síncronos e assíncronos

O diagrama abaixo mostra o fluxo de um pedido.

```
Cliente (JWT)
   │  POST /orders
   ▼
Order Service ──REST (retry, timeout, circuit breaker)──► Catalog Service
   │                                                     (reserva de estoque)
   │ 1. chamada síncrona: POST /payments
   ▼
Payment Service ──► resultado publicado no exchange "payments"
   │                      │
   │                      ├── routing key payment.approved ──► fila payment.approved
   │                      └── routing key payment.rejected ──► fila payment.rejected
   │                                                              │
   │                                                              ▼
   │                                                  Notification Service
   │
   │ 2. fallback (Payment indisponível): publica em "orders" / order.created
   ▼
RabbitMQ ──► Payment Service (consumo com ACK, retry e DLQ)
```

O caminho síncrono vai do cliente ao Order e do Order ao Catalog e ao Payment, por REST. O caminho assíncrono começa no fallback: quando o Payment não responde, o Order publica o pedido na fila `order.created`, e o consumidor do Payment processa a mensagem depois. Os resultados saem pelo exchange `payments`, e o Notification consome as filas de resultado.

### 2.3 Padrão de mensagens

Toda mensagem publicada usa um envelope padrão, com `event_id` (UUID v4), `event_name`, `correlation_id`, `timestamp`, `producer_service` e `data`. A mensagem é persistente (`delivery_mode=2`) e as filas são duráveis. O consumidor confirma (ACK) apenas depois de processar. Mensagens inválidas ou com falha repetida vão para a DLQ, sem reenfileiramento. A idempotência usa a chave `event_id`, o que impede cobrança duplicada quando a mesma mensagem chega mais de uma vez.

---

## 3. Decisões técnicas

**REST entre Order e Catalog.** O Order precisa da resposta do catálogo para decidir se o pedido segue. Por isso a comunicação é síncrona, com JSON sobre HTTP, contratos documentados em OpenAPI (`docs/api/`) e códigos de status padronizados.

**Mensageria para o pagamento e as notificações.** O pagamento e o envio de notificações não precisam bloquear a resposta ao cliente. O RabbitMQ desacopla esses passos, mantém as mensagens enquanto o consumidor está fora e permite reprocessar. Escolhemos RabbitMQ pelo painel de administração e pelo suporte nativo a dead letter queues.

**Resiliência.** Três mecanismos atuam juntos:
- **Timeout** por endpoint, configurado em `timeout_config`, para que nenhuma chamada fique presa.
- **Retry** com backoff exponencial e jitter, limitado a duas tentativas nas chamadas ao catálogo.
- **Circuit breaker** por serviço de destino. Após três falhas consecutivas, o circuito abre por dez segundos e as chamadas falham rápido.

Quando o circuito está aberto, o **fallback** responde de forma degradada. Para o catálogo, a resposta usa um cache local. Para o pagamento, o pedido vai para a fila e fica como `PAYMENT_PENDING`.

**Consistência.** Adotamos consistência eventual, com o padrão SAGA coreografado. A reserva de estoque é desfeita por uma operação compensatória (`/release`) quando um passo posterior falha. A justificativa, incluindo a análise de CAP, está em `docs/architecture/consistency_strategy.md.md`.

**Segurança.** Autenticação com JWT assinado por HMAC-SHA256 (`HS256`), validado por middleware em todas as rotas protegidas. A autorização usa RBAC, com papéis que restringem, por exemplo, a criação de produtos. A comunicação entre serviços usa tokens de serviço e chaves internas, separados dos tokens de usuário.

**Observabilidade.** Cada requisição recebe um `X-Correlation-ID`, lido do cabeçalho recebido ou gerado como UUID v4 e devolvido na resposta. O mesmo ID aparece nas mensagens AMQP e nos logs. Os logs são JSON, uma linha por evento, emitidos no stdout. As métricas Prometheus incluem contagem e latência de requisições (com buckets definidos), mensagens processadas por fila e estado dos circuit breakers. O endpoint `/metrics` está em todos os serviços.

**Healthchecks.** Cada serviço expõe `/health/live` (aplicação em execução) e `/health/ready` (dependências acessíveis, como o broker). O Compose usa o readiness para o status `healthy`, e o `depends_on` com `service_healthy` garante a ordem de inicialização.

**Infraestrutura.** A stack é orquestrada por Docker Compose em uma rede bridge própria. Order, Catalog, Payment, Prometheus e Grafana têm limites de CPU e memória. O notification-service ainda não os define. Segredos ficam em `.env`, criado a partir de `.env.example`, e o Compose recusa subir sem as credenciais obrigatórias.

---

## 4. Dificuldades enfrentadas e lições aprendidas

**Rede com DNS restrito.** Na rede da faculdade, os containers de build não resolviam `deb.debian.org`, e o `apt-get update` falhava. Os pulls do Docker Hub também tiveram erros intermitentes (`unexpected EOF`). A solução foi construir as imagens com `docker build --network host`, que usa o DNS do host. A lição é que o ambiente de build precisa ser testado com a mesma rede que será usada na demonstração.

**PRs empilhadas e código perdido.** Algumas entregas foram feitas em PRs empilhadas, com base em outras branches de feature. Quando as bases foram mescladas, o código ficou nessas branches intermediárias, e não em `main`. Métricas inteiras precisaram ser recuperadas e reaplicadas em outra issue. A lição: PRs empilhadas devem ser reapontadas para `main` antes de mesclar a base.

**Testes destruindo dados de desenvolvimento.** Um teardown de teste rodava `docker compose down -v` sem o arquivo de override. Isso removeu os volumes de Prometheus, Grafana e RabbitMQ da stack de desenvolvimento. O override foi corrigido para renomear todos os volumes, e o fixture passou a usar o helper de compose. A lição é que comandos destrutivos em testes precisam usar o mesmo escopo de isolamento do restante da suíte.

**Subida parcial do Compose.** Ao subir um único serviço para aplicar uma mudança, o Compose removeu containers de outros serviços, que não voltaram. A recuperação foi subir a stack inteira. A lição é preferir sempre a stack completa quando o arquivo de composição muda.

**Divergência entre ambientes de tipagem.** O CI instalava uma versão do pika com tipos mais rígidos do que a usada localmente. O mypy passava na máquina e falhava no pipeline. A correção foi alinhar as versões e tipar explicitamente `routing_key`, `delivery_tag` e o corpo das mensagens.

**Integração no CI.** A suíte de integração sobe a stack com `--wait`, e isso expôs três problemas: o notification-service não tinha as credenciais exigidas pelo Compose, o override de teste trocava o servidor HTTP por um `sleep` e tornava o healthcheck inválido, e o fixture do DLQ não passava o Grafana. Cada caso foi corrigido na configuração de teste.

**Histórico de commits.** Commits com a linha de coautoria de uma ferramenta de IA foram reescritos localmente. O push forçado foi bloqueado pelas regras de segurança do ambiente, e por isso o histórico remoto de algumas branches ainda contém essas linhas. A lição é combinar a política de autoria antes de começar a escrever commits.

**Lições gerais.** Os problemas mais caros foram os que ficaram invisíveis até a integração: DNS no build, dados perdidos por comandos destrutivos em testes e divergência de versões entre ambientes. Validar a stack completa com a mesma rede e as mesmas credenciais da demonstração teria evitado a maior parte deles.

---

## 5. Divisão de responsabilidades

A matriz abaixo foi montada a partir das atribuições das issues e da autoria dos Pull Requests no GitHub. Cada integrante deve revisar a própria linha.

| Integrante | Issues principais | Pull Requests principais |
|---|---|---|
| Quezia Costa | Infraestrutura base (#1), Compose e redes (#2), configurações (#3), Catalog (#5), Order (#6), cliente HTTP (#9), JWT (#17), interceptor Bearer (#18) | #31, #32, #40, #53, #57, #58, #59, #62, #64 |
| Lettycia Vitória | Payment (#7), timeout (#13), circuit breaker (#15), tokens de serviço (#20), healthchecks (#26), teste de caos (#28), contratos OpenAPI (#29) | #41 a #48, #50, #63, #65 |
| Lucas Mendes Nóbrega | Broker RabbitMQ (#4), Notification (#8), produtor de eventos (#10), correlation ID (#21), logs JSON (#22), métricas (#23), `/metrics` (#24), Prometheus e Grafana (#61) | #33 a #39, #67 |
| João Victor | Consumo e DLQ (#11), idempotência (#12), retry (#14), fallback (#16), RBAC (#19), SAGA (#25), testes (#27) | #49, #51, #52, #54, #55, #56, #60, #66 |

**Assinaturas:** Quezia Costa ______________  Lettycia Vitória ______________  Lucas Mendes Nóbrega ______________  João Victor ______________

---

## 6. Uso de ferramentas de IA

Conforme as regras da disciplina, declaramos o uso de ferramentas de IA no projeto.

**Ferramenta:** Claude Code, com o modelo Claude Haiku 5.5 (Anthropic).

**Onde foi usada:**
- escrita e revisão de código dos componentes de middleware, dos serviços e dos testes;
- configuração do Docker Compose, do Prometheus e do Grafana;
- diagnóstico de falhas de build, de testes e de CI;
- redação de documentação, incluindo este relatório;
- operações de Git, como merge, resolução de conflitos e reescrita de mensagens de commit.

**Principais solicitações feitas à ferramenta** (resumidas, não literais):
- implementar os componentes definidos em cada issue e validar o resultado com testes e com a stack em Docker;
- diagnosticar e corrigir falhas de CI, como erros de tipagem e configuração de testes de integração;
- montar o dashboard do Grafana e a configuração de scraping do Prometheus;
- resolver conflitos de merge com a branch principal, preservando o trabalho dos colegas;
- gerar documentação de histórico e de arquitetura a partir do estado real do repositório.

**Responsabilidade:** o grupo revisou, entendeu e validou o código e os documentos gerados. As decisões de arquitetura, as escolhas de implementação e o conteúdo deste relatório são de responsabilidade da equipe.
