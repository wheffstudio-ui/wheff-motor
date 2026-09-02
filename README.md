# WHEFF — Motor

Trabalho pesado da plataforma Wheff: baixar posts, transcrever, extrair cenas.

O cérebro (estado, fila, dependências) fica no Supabase.
Este repositório é só o braço: pega tarefa da fila, executa, devolve o resultado.

```
Supabase (fila)  →  GitHub Actions  →  Supabase (artefatos)
```

## O que roda aqui

| Tarefa | O que faz |
|---|---|
| `media.ingest` | yt-dlp baixa o post, faster-whisper transcreve |

## Segredos

Configurados em Settings → Secrets and variables → Actions:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

**Este repositório é público e não contém segredo nenhum.**
Nenhum dado de campanha, cliente ou marca passa por aqui — só o link do post
que está sendo analisado e o resultado, que vai direto para o banco.
