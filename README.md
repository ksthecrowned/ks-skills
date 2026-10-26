# ks-skills

Marketplace de plugins [Claude Code](https://code.claude.com) : des skills réutilisables sur n'importe quel projet.

## Installation

Dans Claude Code :

```text
/plugin marketplace add ksthecrowned/ks-skills
/plugin install audit-documentation@ks-skills
```

Ou depuis un terminal :

```bash
claude plugin marketplace add ksthecrowned/ks-skills
claude plugin install audit-documentation@ks-skills
```

Mise à jour : `claude plugin marketplace update ks-skills`.

## Plugins

| Plugin | Ce qu'il fait |
|--------|---------------|
| [audit-documentation](./plugins/audit-documentation/) | Évalue la documentation d'un projet et corrige les endroits où elle contredit le code. Script de détection multi-stack, barème de gravité, score sur 100. |

## Ajouter un skill

1. Créer `plugins/<nom>/.claude-plugin/plugin.json` (même `name` que l'entrée de la marketplace).
2. Mettre le skill dans `plugins/<nom>/skills/<nom>/SKILL.md`, avec ses scripts et références à côté.
3. Ajouter une entrée dans `.claude-plugin/marketplace.json`.
4. Vérifier : `claude plugin validate .` puis `claude plugin validate ./plugins/<nom>`.

## Licence

[MIT](./LICENSE)
