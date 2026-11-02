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
| `open <id>` | abre o arquivo (no navegador, se estiver no Drive) |
| `drive login` | conecta sua conta do Google Drive |
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

**Cloud Bridge (Google Drive).** Vídeos e arquivos compactados acima de `cloud_min_mb` (padrão 200 MB) são **enviados direto para o seu Google Drive pela internet**, sem precisar instalar o app do Drive. Eles ficam em `Meu Drive/FileLife/<Categoria>`, a cópia local é apagada e, no lugar, fica um atalho que abre o arquivo no navegador (drive.google.com). Você também pode abrir com o comando `open <id>`. Os envios rodam em segundo plano. Se um envio falhar, o arquivo continua no PC.

Quando expiram, os arquivos vão para `Meu Drive/FileLife/_ArquivoFrio` (`on_expire: "archive"`) ou para a lixeira do Drive (`"delete"`), onde dá para recuperar por 30 dias.

O FileLife só consegue ver e mexer nos arquivos que ele mesmo enviou (permissão `drive.file`). O resto do seu Drive fica inacessível para ele.

### Configurar o Google Drive (uma vez, ~5 min)

1. Acesse https://console.cloud.google.com/ e crie um projeto (ex.: "FileLife").
2. Em **APIs e serviços → Biblioteca**, ative a **Google Drive API**.
3. Em **APIs e serviços → Tela de permissão OAuth**, escolha "Externo", preencha o nome do app e o seu e-mail e, em **Usuários de teste**, adicione o seu Gmail.
4. Em **APIs e serviços → Credenciais → Criar credenciais → ID do cliente OAuth**, escolha o tipo **"App para computador"**.
5. Baixe o JSON e salve em `~/.filelife/google_credentials.json` (no Windows: `C:\Users\SEU_USUARIO\.filelife\google_credentials.json`).
6. Rode `pip install -r requirements.txt`, abra o FileLife e digite `drive login`. O navegador abre, você entra na sua conta Google e autoriza. Pronto: o login fica salvo.

> Para vender o produto: os passos 1 a 5 são feitos **só por você**, e o seu `google_credentials.json` vai junto com o programa. Seus clientes só fazem o passo 6 (login no navegador). Com mais de 100 usuários, publique o app na tela de permissão OAuth.

**Alternativa com o app do Drive instalado:** defina `"cloud_mode": "pasta"`. Nesse modo, o FileLife usa a pasta sincronizada (`G:\Meu Drive`, OneDrive ou Dropbox) em vez da API. Configure o app em "Fazer streaming de arquivos" para os arquivos não ocuparem o SSD. Para preferir OneDrive ou Dropbox, defina `"cloud_provider": "outro"`.

Para mandar mais coisas para a nuvem, diminua `cloud_min_mb` ou acrescente categorias em `cloud_categories`.
