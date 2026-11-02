# FileLifecycle Pro

Sistema de Gestão de Ciclo de Vida de Arquivos: monitora a pasta **Downloads**, categoriza, renomeia com timestamp, move para `~/OrganizedFiles/<Categoria>` e registra uma **data de expiração** em SQLite. Arquivos expirados são removidos automaticamente.

## Uso

```bash
python filelifecycle.py                      # monitora ~/Downloads em segundo plano
python filelifecycle.py --organize-existing  # organiza também o que já está lá
python filelifecycle.py --watch D:/OutraPasta
```

O núcleo roda só com Python 3.8+. Extras opcionais:

```bash
pip install -r requirements.txt
```

- `anthropic`: resumo com IA. Também precisa da variável `ANTHROPIC_API_KEY` (crie uma chave em https://platform.claude.com).
- `pyzipper`: zips do Arquivo Frio com senha AES.

Ele não pergunta nada a cada download: cada categoria tem um prazo padrão (editável em `~/.filelife/config.json`) e você ajusta pelo terminal:

| Comando | O que faz |
|---|---|
| `status` | resumo e próximas expirações |
| `list [categoria]` | lista arquivos ativos com seus IDs |
| `extend <id> <prazo>` | novo prazo (`3d`, `2w`, `1m`, `1y`) |
| `keep <id>` | nunca expirar |
| `expire <id>` | expira agora |
| `versions <id>` / `restore <id> <n>` | histórico de versões |
| `archive <id>` | local do zip e senha |
| `summary <id>` | gera ou refaz o resumo com IA |
| `cloud <id>` | envia para a nuvem e deixa um atalho |
| `cleanup` | processa expirados agora |
| `report [dias]` | gera relatório de produtividade |

Downloads em andamento (`.crdownload`, `.part`…) são ignorados, e um arquivo só é processado quando o tamanho dele para de mudar.

## Empacotar

```bash
pip install pyinstaller
pyinstaller --onefile filelifecycle.py
```

## 7 funções "Foda"

Todas implementadas ✅

1. ✅ **Dedupe Inteligente** — detecta arquivos repetidos por hash (MD5/SHA) e mantém só uma versão. *"Nunca mais tenha 5 versões do mesmo PDF no seu PC."*
2. ✅ **Resumo de Conteúdo (IA)** — gera resumo de 3 bullets para PDFs/Docs em um `.md` ao lado. *"Baixou um relatório de 40 páginas? Eu já li e resumi o que importa."*
3. ✅ **Auto-Compressão (Arquivo Frio)** — expirados podem ir para `.zip` com senha gerada. *"Seu histórico não é lixo, é memória."*
4. ✅ **Cloud Bridge** — arquivos grandes vão para Google Drive/OneDrive e viram atalho local. *"Seu SSD agradece."*
5. ✅ **Tags de Prioridade** — prefixos como `[URGENTE]`, `[ARQUIVO]`, `[LEITURA]`. *"Sua pasta de Downloads vira um dashboard de prioridades."*
6. ✅ **Backup Diferenciado (Versioning)** — salva versão anterior ao modificar arquivos em `OrganizedFiles`. *"Esqueceu como era aquele contrato antes da última edição?"*
7. ✅ **Relatório de Produtividade** — PDF semanal com estatísticas de arquivos, tempo e GB liberados. *"Prova de que seu PC está mais limpo."*

## Detalhes das funções 2 e 4

**Resumo com IA.** PDFs, `.docx` e arquivos de texto da categoria Documentos são resumidos em 3 tópicos pelo Claude (modelo `claude-opus-5-5`, configurável em `ai_model`). O resumo fica salvo em `<arquivo>.resumo.md`, ao lado do original. Ele roda em segundo plano, sem travar o monitor. O fallback automático da API (`fallbacks: "default"`) está ativado: se o modelo recusar um conteúdo, outro modelo tenta resumir. Arquivos acima de `ai_max_mb` (padrão 30 MB) são pulados. Para desligar, use `"ai_summary": false`.

**Cloud Bridge.** Vídeos e arquivos compactados acima de `cloud_min_mb` (padrão 200 MB) vão para a pasta `FileLife/` dentro da pasta sincronizada do Google Drive, OneDrive ou Dropbox, que é detectada automaticamente (ou definida em `cloud_dir`). No lugar do arquivo fica um atalho: `.url` no Windows e link simbólico no Mac/Linux. Quando expiram, eles são arquivados dentro da própria nuvem.
> Para liberar espaço de verdade no SSD, deixe o app da nuvem no modo "streaming" ou "Arquivos sob demanda". Assim o arquivo fica só online até você abrir.
