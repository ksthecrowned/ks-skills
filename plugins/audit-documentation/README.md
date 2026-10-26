# audit-documentation

Un skill Claude Code qui répond à « que penses-tu de la doc de ce projet ? » avec des preuves, puis corrige les endroits où la documentation contredit le code.

## Ce qu'il fait

1. **Évaluer** (sans rien modifier). Un script lit la doc et le code, puis signale les écarts :
   - unités de code (modules, packages, apps) absentes de la carte des modules, ou citées alors qu'elles ont été supprimées ;
   - chemins et liens cités qui n'existent plus ;
   - commandes citées (`npm run …`, `make …`, `just …`) qui n'existent pas ;
   - fichiers promis par la doc mais absents (CHANGELOG, LICENSE…) ;
   - routes d'API sans description OpenAPI (NestJS, FastAPI, Spring) ;
   - chemins absolus vers une autre machine, secrets suivis par git ;
   - dates de vérification posées en masse, docs figées pendant que le code bouge, consignes recopiées, specs sans statut.

   Chaque constat a une gravité (bloquant / important / mineur) et une preuve. Le projet reçoit un score sur 100. Claude vérifie ensuite chaque constat, ajoute ce qu'un script ne peut pas juger, et rend un rapport.
2. **Corriger**, si on le lui demande. Claude lit les conventions du projet, vérifie les effets de bord (par exemple sur le découpage de la doc Swagger par audience), fait des modifications ciblées, relance le script et rend compte du niveau de vérification atteint.

## Installation

```text
/plugin marketplace add ksthecrowned/ks-skills
/plugin install audit-documentation@ks-skills
```

## Utilisation

Dans un projet, demander simplement :

```text
Que penses-tu de la documentation de ce projet ?
```

puis, après le rapport :

```text
Corrige les endroits où la doc contredit le code.
```

Le skill peut aussi être appelé directement : `/audit-documentation:audit-documentation`.

Le script s'utilise aussi seul (Python 3.8+, aucune dépendance, lecture seule) :

```bash
python3 skills/audit-documentation/scripts/doc_audit.py /chemin/du/projet
python3 skills/audit-documentation/scripts/doc_audit.py /chemin/du/projet --json --out audit.json
```

## Stacks couvertes

Node (NestJS, Express, Fastify, Next.js), Python (FastAPI, Django, Flask), Go, PHP (Laravel, Symfony), Ruby (Rails), Java / Kotlin (Spring), monorepos (pnpm, npm workspaces, Turborepo, Nx, go.work). Ailleurs, le script se rabat sur les sous-dossiers du dossier source principal.

## Limites

Le script est heuristique : il produit des pistes, pas des verdicts. Il ne juge pas l'exactitude du contenu (routes, rôles, flux décrits) ni la qualité rédactionnelle ; c'est le rôle de Claude dans la suite du skill. Le score mesure la cohérence doc ↔ code détectable automatiquement, pas la qualité globale de la documentation.

## Contenu

```
skills/audit-documentation/
├── SKILL.md                 # déroulé de l'audit et des corrections
├── scripts/doc_audit.py     # détection des écarts
└── references/
    ├── severity.md          # barème de gravité et score
    └── stacks.md            # repères par stack
```
