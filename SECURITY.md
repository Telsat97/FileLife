# Segurança do FileLife

## Como relatar uma falha
Abra uma issue **sem detalhes técnicos** pedindo contato, ou use "Report a vulnerability" na aba Security do GitHub. Não publique a falha antes da correção.

## Como o FileLife Web se protege

| Risco | Proteção |
|---|---|
| Vazamento de dados num servidor | Não existe servidor do FileLife. Os arquivos vão do navegador direto para o Drive do usuário. |
| Acesso indevido ao Drive | Permissão `drive.file`: o FileLife só enxerga o que ele mesmo criou. O token fica só na memória da aba e é revogado ao clicar em "Sair". |
| Outro site usando o login do FileLife | O ID do cliente do Google só funciona na origem autorizada (`https://telsat97.github.io`). |
| Injeção de código (XSS) por nome de arquivo ou resposta da IA | A tela nunca monta HTML a partir de dados de fora (só `textContent`). A política de segurança (CSP) só permite scripts do próprio site e do login oficial do Google. |
| Dados enviados para lugares estranhos | A CSP só permite conexões com `googleapis.com`, o login do Google e `api.anthropic.com`. |
| Clickjacking (site embutido em outro) | O site se recusa a funcionar dentro de molduras de outros sites. |
| Perda de arquivos | Nada é apagado de vez: expirados vão para o Arquivo Frio ou para a lixeira do Drive (30 dias). A cópia do PC só é removida depois de o Drive confirmar a mesma impressão digital SHA-256, e se o arquivo não mudou durante o envio. |
| Envio interrompido | Envio em partes, retomado de onde parou. |
| Configuração corrompida | Valores inválidos voltam ao padrão seguro. |
| Chave da IA exposta | É opcional, de cada usuário, e fica só no navegador dele (por padrão, some ao fechar a aba). |
| Ferramentas de publicação adulteradas | As GitHub Actions estão travadas pelo código exato da versão (SHA), e a publicação verifica que não há segredos em `web/`. |

## Limites conhecidos (honestidade)
- O GitHub Pages não permite configurar cabeçalhos HTTP de segurança. A CSP é aplicada por `<meta>`, que cobre scripts, conexões e imagens, mas não `frame-ancestors`; isso é coberto pelo bloqueio em JavaScript. Para cabeçalhos completos, coloque o site atrás do Cloudflare (grátis).
- Se alguém tomar a sua conta do GitHub, pode alterar o site. **Ative a verificação em duas etapas no GitHub e no Google.**
- A chave da Anthropic, quando usada, fica acessível ao JavaScript do próprio site (exigência para chamar a API pelo navegador).
- Um site não consegue vigiar pastas do computador com a aba fechada (proteção dos próprios navegadores).
