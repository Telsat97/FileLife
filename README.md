# FileLifecycle Pro

Sistema de Gestão de Ciclo de Vida de Arquivos: monitora a pasta **Downloads**, categoriza, renomeia com timestamp, move para `~/OrganizedFiles/<Categoria>` e registra uma **data de expiração** em SQLite. Arquivos expirados são removidos automaticamente.

## Uso

```bash
python filelifecycle.py                      # monitora ~/Downloads em segundo plano
python filelifecycle.py --organize-existing  # organiza também o que já está lá
python filelifecycle.py --watch D:/OutraPasta
```

Sem dependências externas (só Python 3.8+). Opcional: `pip install pyzipper` para zips do Arquivo Frio com senha AES.

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
| `cleanup` | processa expirados agora |
| `report [dias]` | gera relatório de produtividade |

Downloads em andamento (`.crdownload`, `.part`…) são ignorados, e um arquivo só é processado quando o tamanho dele para de mudar.

## Empacotar

```bash
pip install pyinstaller
pyinstaller --onefile filelifecycle.py
```

## 7 funções "Foda"

Status: ✅ implementada · 🔜 próxima

1. ✅ **Dedupe Inteligente** — detecta arquivos repetidos por hash (MD5/SHA) e mantém só uma versão. *"Nunca mais tenha 5 versões do mesmo PDF no seu PC."*
2. 🔜 **Resumo de Conteúdo (IA)** — gera resumo de 3 bullets para PDFs/Docs em um `.md` ao lado. *"Baixou um relatório de 40 páginas? Eu já li e resumi o que importa."*
3. ✅ **Auto-Compressão (Arquivo Frio)** — expirados podem ir para `.zip` com senha gerada. *"Seu histórico não é lixo, é memória."*
4. 🔜 **Cloud Bridge** — arquivos grandes vão para Google Drive/OneDrive e viram atalho local. *"Seu SSD agradece."*
5. ✅ **Tags de Prioridade** — prefixos como `[URGENTE]`, `[ARQUIVO]`, `[LEITURA]`. *"Sua pasta de Downloads vira um dashboard de prioridades."*
6. ✅ **Backup Diferenciado (Versioning)** — salva versão anterior ao modificar arquivos em `OrganizedFiles`. *"Esqueceu como era aquele contrato antes da última edição?"*
7. ✅ **Relatório de Produtividade** — PDF semanal com estatísticas de arquivos, tempo e GB liberados. *"Prova de que seu PC está mais limpo."*
