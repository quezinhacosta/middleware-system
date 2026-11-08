# Middleware Distribuído — E-Commerce System

Projeto prático desenvolvido para a disciplina de **Arquitetura de Sistemas Distribuídos**, vinculada ao curso de Ciência da Computação da **Universidade Católica de Pernambuco (UNICAP)**, sob orientação do **Prof. Pedro Ximenes**.

---

##  Integrantes da Equipe

* **Quezia Costa**
* **Lettycia França**
* **Lucas Mendes**
* **João Victor**

---

## Proposta e Escopo do Projeto

O objetivo central deste projeto é **projetar e implementar do zero uma camada de middleware distribuído** capaz de orquestrar a comunicação, resiliência, segurança e observabilidade entre microsserviços heterogêneos de uma aplicação real de **E-commerce**.

O foco pedagógico e técnico está na **camada de middleware** e na resolução de desafios inerentes a sistemas distribuídos (falhas parciais de rede, latência, inconsistência eventual e rastreabilidade), mantendo a lógica de negócio dos serviços concisa e modular. Conforme as diretrizes do projeto, **não são utilizados API Gateways ou middlewares prontos de mercado** (como Kong, Istio, MuleSoft ou AWS API Gateway) para a lógica central, garantindo que os padrões de distribuição sejam implementados e compreendidos diretamente pelo time.

---

##  Domínio da Aplicação: E-Commerce

O domínio modelado contempla o ciclo de vida completo de compras online, particionado em microsserviços autônomos que operam em containers dedicados:

1. **Order Service (Serviço de Pedidos)**:
   * Ponto de entrada das requisições de clientes.
   * Criação, consulta e gerenciamento do ciclo de vida dos pedidos.
   * Realiza chamadas síncronas para validação de catálogo e emite eventos assíncronos ao fechar um pedido.

2. **Catalog Service (Serviço de Catálogo & Estoque)**:
   * Gestão de itens, preços e reserva temporária de estoque.
   * Responde a consultas síncronas de validação solicitadas pelo serviço de pedidos.

3. **Payment Service (Serviço de Pagamento)**:
   * Processamento transacional de pagamentos (cartão, PIX, boleto).
   * Consome solicitações de cobrança e publica confirmações/recusas no barramento de mensageria.

4. **Notification Service (Serviço de Notificações & Pós-Venda)**:
   * Consumidor assíncrono que reage aos eventos de pedidos confirmados ou cancelados para envio de e-mails/alertas simulados ao cliente.

---

##  Arquitetura da Solução e Camada de Middleware

```
                              [ Cliente / Frontend ]
                                        │
                                        ▼ (HTTP / JWT)
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │                         MIDDLEWARE DISTRIBUÍDO                              │
 │                                                                             │
 │  ┌───────────────────────┐  ┌─────────────────────┐  ┌───────────────────┐  │
 │  │ Segurança & Auth      │  │ Observabilidade     │  │ Resiliência       │  │
 │  │ (JWT & RBAC Roles)    │  │ (Logs JSON & Tracing│  │ (Retry Exponencial│  │
 │  │                       │  │  Correlation-ID)    │  │  Timeout, Circuit │  │
 │  │                       │  │                     │  │  Breaker/Fallback)│  │
 │  └───────────────────────┘  └─────────────────────┘  └───────────────────┘  │
 └──────────────────────┬───────────────────────────────┬──────────────────────┘
                        │                               │
        (Síncrono REST) │               (Síncrono REST) │
                        ▼                               ▼
             ┌─────────────────────┐         ┌─────────────────────┐
             │    Order Service    │         │   Catalog Service   │
             │     (Container)     │         │     (Container)     │
             └──────────┬──────────┘         └─────────────────────┘
                        │
                        │ (Publica evento de pedido criado)
                        ▼
             ┌─────────────────────────────────────────────────────┐
             │       Message Broker (RabbitMQ / Redis Queue)       │
             └──────────┬───────────────────────────────┬──────────┘
                        │                               │
                        ▼ (Consome evento de pagamento) ▼ (Consome evento de status)
             ┌─────────────────────┐         ┌─────────────────────┐
             │   Payment Service   │         │Notification Service │
             │     (Container)     │         │     (Container)     │
             └─────────────────────┘         └─────────────────────┘
```

### Capacidades Centrais do Middleware Desenvolvido

| Capacidade | Implementação no Projeto |
| :--- | :--- |
| **Comunicação Síncrona** | APIs RESTful documentadas com contratos claros e códigos HTTP padronizados entre os serviços de Pedidos e Catálogo. |
| **Comunicação Assíncrona** | Fila de mensageria / Pub-Sub (RabbitMQ/Redis) desacoplando o fechamento de pedidos do processamento de pagamentos e notificações. |
| **Resiliência** | Mecanismos de *Retry* com backoff exponencial, *Timeouts* granulares por rota, *Circuit Breaker* para evitar efeito cascata e *Fallback* degradado. |
| **Observabilidade** | Geração e propagação de `X-Correlation-ID` entre saltos de rede, logs estruturados em JSON e métricas de latência e contagem de erros. |
| **Segurança** | Autenticação via token JWT e autorização por papéis (*Role-Based Access Control* - RBAC) diferenciando rotas de clientes e administradores. |
| **Tolerância a Falhas** | Estratégia documentada de consistência eventual e capacidade de degradação graciosa com recuperação automática de containers. |

---

##  Estrutura do Repositório

```text
middleware-system/
├── docker-compose.yml          # Orquestração de todos os containers e redes
├── README.md                   # Documentação principal e guia de execução
├── docs/                       # Relatório técnico, diagramas e atas de projeto
│   └── architecture/           # Diagramas conceituais e contratos de API
├── middleware/                 # Módulos centrais reutilizáveis do middleware
│   ├── auth/                   # Validação de tokens JWT e verificação de RBAC
│   ├── resilience/             # Implementações de Circuit Breaker, Retry e Fallback
│   ├── observability/          # Interceptadores de Correlation ID e formatador de logs JSON
│   └── transport/              # Adaptadores de clientes HTTP e conexão de mensageria
└── services/                   # Microsserviços da aplicação de e-commerce
    ├── order-service/          # Microsserviço de gestão de pedidos
    ├── catalog-service/        # Microsserviço de catálogo e estoque
    ├── payment-service/        # Microsserviço de processamento de pagamentos
    └── notification-service/   # Microsserviço consumidor de eventos pós-compra
```

---

##  Como Executar o Projeto

### Pré-requisitos
* [Docker](https://docs.docker.com/get-docker/) (v20.10 ou superior)
* [Docker Compose](https://docs.docker.com/compose/) (v2.0 ou superior)
* Git instalado

### Execução dos Containers
Para inicializar toda a malha de serviços, o middleware e a infraestrutura de mensageria com um único comando:

```bash
# Clonar o repositório
git clone https://github.com/<usuario>/middleware-system.git
cd middleware-system

# Subir todos os serviços em background
docker compose up --build -d

# Visualizar o status e saúde dos containers
docker compose ps

# Acompanhar os logs estruturados de todos os serviços em tempo real
docker compose logs -f

#Para derrubar
docker compose down 
```
---

##  Política de Uso 

Em estrito cumprimento às diretrizes acadêmicas do projeto:
* O uso de ferramentas de IA (ex: Claude, ChatGPT, Gemini, Copilot) é realizado de forma consciente para apoio em ideação, estruturação arquitetural, documentação e testes.
* Toda a lógica gerada é revisada, validada e compreendida integralmente pela equipe.
* O relatório técnico final conterá a seção obrigatória discriminando as ferramentas utilizadas, os prompts fundamentais aplicados e a respectiva contribuição no ciclo de desenvolvimento.
* Os commits realizados apresentam consciência e descrição breve do que foi realizado especificamente naquele commit
* Pull Requests são utilizadas obrigatoriamente. Tanto para compreensão geral do grupo sobre o que está sendo feito, tanto para controle de versão e funcionamento de código
