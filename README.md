# glm-critic

Um juiz de rubrica para fluxos de conteúdo. Recebe itens de uma fonte (hoje um
leitor RSS self-hosted), pontua cada um contra um critério de valor **explícito
e versionado** usando um modelo de linguagem, e devolve só o que passa do corte.

Sem dependências de runtime: stdlib puro. Testes unitários rodam sem rede.

```bash
glm-critic run --limit 40 --dry-run     # julga e mostra, sem gravar nada
glm-critic run --limit 200 --star       # julga e marca o sinal na fonte
glm-critic rubric                       # imprime a rubrica em vigor
glm-critic serve --port 8080            # endpoint para o n8n
```

## O problema

Um leitor de RSS resolve **armazenar e ler**. Não resolve **decidir**. Semeado
com 45 feeds, o Miniflux mostrou 3181 não lidos em uma hora — e a lista era
dominada por posts de 2012. Nada ali estava errado: era o histórico do feed
virando fila de leitura. Mesmo depois de limpar, ~3000 itens por recarga não são
leitura.

O trabalho que sobra é de julgamento, e é o que este projeto faz.

## As quatro decisões

### 1. A rubrica é código, não prompt

`src/glm_critic/rubric.py` contém o critério: um item vale quando aponta um
**artefato primário verificável**, **muda uma decisão num projeto nomeado**, e
**declara o efeito com número** (ou diz que não mediu). Falhando o primeiro é
boato; o segundo, cultura geral; o terceiro, marketing.

Discordar do juiz é editar esse arquivo — o diff fica no git e a mudança de
comportamento é auditável. A rubrica também **entra no hash do veredito**: mudar
o critério invalida o cache automaticamente, em vez de misturar duas réguas no
mesmo placar sem avisar.

O critério não foi inventado aqui: veio de um acervo real de mais de mil
entradas curadas à mão, das quais sobreviveram as que apontavam para artefatos —
e cinco tiveram de ser corrigidas porque o resumo de terceiro estava errado ou
invertia a direção do efeito.

### 2. Deduplicar antes de pagar o juiz

O mesmo paper chega pelo arXiv, pelo HuggingFace Daily Papers e por uma
newsletter. Julgar três vezes paga três vezes e pode devolver três vereditos que
discordam — o que é pior que o custo, porque destrói a confiança no placar.

`dedup.py` cola título normalizado idêntico **e** prefixo com sufixo curto
(`Recursive Language Models` ↔ `Recursive Language Models — curto`). Sufixo
longo **não** cola, e essa fronteira é testada.

### 3. Nunca inventar alinhamento

O juiz devolve texto livre que deveria ser um array JSON. Ele às vezes embrulha
em cerca de código, às vezes usa vírgula final, às vezes omite o índice. Cada um
desses casos tem conserto barato.

O que **não** tem conserto é atribuir um veredito ao item errado. Por isso:
índice duplicado significa ambiguidade real e o parser cai para a posição; uma
resposta sem forma reconhecível de veredito é descartada em vez de virar "ruído"
por posição — sem esse corte, o juiz falharia e o item bom seria marcado como
ruído **em silêncio**.

**Um lote perdido é ruído visível; um veredito no item errado é erro
silencioso.** O código prefere o primeiro.

### 4. O veredito volta para onde o conteúdo está

A biblioteca não sabe onde o conteúdo mora: fonte e destino entram por
adaptador. Hoje o destino é marcar o sinal no próprio leitor, para a visão
"Starred" ser a lista curta, em vez de existir uma segunda tela para ler.

## Custo, medido

Uma rodada de 12 itens gastou **4.101 tokens de entrada e 6.235 de saída** —
cerca de 340 de entrada e 520 de saída por item.

A saída domina porque é modelo de raciocínio: numa chamada de teste com
`max_tokens=40`, 39 foram raciocínio e o conteúdo voltou **vazio**. Cortar o
orçamento não economiza — produz lote ilegível, que é dinheiro pago e perdido.

## Configuração

Nada de segredo ou URL de deploy no código; tudo por ambiente, e o que falta
falha alto.

| Variável | Obrigatória | Para quê |
|---|---|---|
| `JUDGE_API_KEY` | sim para `run`/`serve` | chave do modelo juiz |
| `JUDGE_URL` | não | URL completa do endpoint; padrão Z.AI Coding Plan |
| `JUDGE_API` | não | `chat` (padrão), `responses`, `anthropic` |
| `JUDGE_MODEL` | não | modelo exato do endpoint; padrão `glm-5.3` |
| `SOURCE_TYPE` | não | `miniflux` (padrão) ou `freshrss` (Fever, unread-only) |
| `SOURCE_URL` / `SOURCE_API_KEY` | sim para `run` | a fonte de conteúdo |
| `SERVICE_TOKEN` | sim para `serve` | **sem ele o serviço não sobe** |
| `CRITIC_EVERY_SECONDS` | não | ciclo autônomo, desligado por padrão (`0`) |
| `NOTIFY_URL` / `NOTIFY_TOKEN` | juntos | webhook interno, autenticação `X-Critic-Token` |
| `CRITIC_MIN_SCORE` | não | corte para virar sinal (padrão 6) |
| `CRITIC_LOG` | não | log de vereditos (padrão `verdicts.jsonl`) |
| `CRITIC_PROJECTS` | não | projetos vivos, separados por vírgula |

## O serviço

```
GET  /health   → 200 sem autenticação (é o health check do Dokku)
POST /run      → exige X-Critic-Token; corpo {"limit":60,"star":true}
```

Só stdlib. **Sem `SERVICE_TOKEN` ele se recusa a subir** — um serviço que decide
em qual rede está para saber se precisa de autenticação acaba sem autenticação
nenhuma.

## Integração com n8n

O critic agenda e chama o webhook **autenticado** do n8n pela rede interna.
Não publique o critic no nginx, Cloudflare ou em portas do host. O leitor é a
única interface de leitura. `CRITIC_EVERY_SECONDS=0` mantém o ciclo desligado.

O fluxo vive em [`yolo-labz/n8n-flows`](https://github.com/yolo-labz/n8n-flows).
O node Webhook precisa de `webhookId` para registrar `/webhook/glm-critic`;
sem esse campo, n8n prefixa o caminho com ID do workflow e nome do node.
`NOTIFY_TOKEN` corresponde à credencial Header Auth `X-Critic-Token` do n8n.

As confirmações ficam em `<CRITIC_LOG>.notified` no mesmo volume. Sinais em cache
não são reenviados após confirmação HTTP. Um timeout depois do envio pode ainda
duplicar e-mail: não há promessa de exactly-once. Sem confirmação, tenta de novo.
Não execute réplicas concorrentes ou CLI junto do scheduler no mesmo log.

## FreshRSS e provedores

O critic completo continua em uso — substituir pelo prompt de uma extensão que
retorna só tags perderia dedup, explicações e trilha. FreshRSS usa a API Fever:
`SOURCE_URL` é a raiz do leitor, `SOURCE_API_KEY` é MD5 de `usuário:senha-da-API`
(não a senha de login). API deve estar habilitada. Use log separado por leitor.
O adaptador pagina em blocos de 50; estrela com `saved`, não toggle.

| Protocolo | Uso | Verificação |
|---|---|---|
| `chat` | Z.AI, endpoints OpenAI-compatible, Ollama/LocalAI compatíveis | contrato local + GLM-5.3 real |
| `responses` | OpenAI `/v1/responses` | contrato local; não comprova acesso/assinatura |
| `anthropic` | Anthropic `/v1/messages` | contrato local; pagamento pausado, não chamado |

Selecione endpoint, protocolo, modelo frontier e chave **explicitamente**. Não há
fallback automático nem conversão de assinatura ChatGPT/Claude em crédito de API.
Compatibilidade de protocolo não garante os recursos/opções de todo modelo.
Somente feeds públicos podem ir à Z.AI; conteúdo privado exige provedor elegível.

O cache é isolado por rubrica, modelo, protocolo, endpoint e leitor. `dry_run`
funciona tanto no HTTP como na CLI: chama o juiz, mas não grava nem estrela.
Falhas parciais saem como erro, não `ok:true`. O uso contabilizado inclui a
primeira resposta em prosa que precisou de retry.

Instalação isolada, migração e gate de produção: [deploy/README.md](deploy/README.md).
Evidência medida: [specs/001-freshrss/evidence.md](specs/001-freshrss/evidence.md).

## O que **não** está medido

- **A precisão.** Não se sabe quantos dos itens marcados valiam a leitura. Numa
  amostra de 12, dois passaram o corte; nenhum foi aberto.
- **A calibração.** O log existe para permitir comparar veredito com o que foi
  lido de fato, mas **a comparação não foi feita** — o corte de 6 é escolha à
  mão, e está declarado como tal.
- **O ganho sobre uma triagem determinística.** Regras de palavra-chave custam
  zero; isto custa tokens. Enquanto os dois não forem comparados sobre o mesmo
  conjunto, essa é a pergunta aberta antes de confiar no gasto.

## Testes

```bash
pip install -e . && python -m unittest discover -s tests -v
```

Casos sem rede: colapso de duplicata e sua fronteira, alinhamento de
veredito (incluindo ambíguo e índice inválido), escopo do cache pela rubrica,
downgrade de lote perdido, e o serviço recusando subir sem token.

## Licença

MIT.
